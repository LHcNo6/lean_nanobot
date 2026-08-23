"""step88：MCP SSE 传输 + 自动重连单元测试。"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from step116.tools.mcp import (
    MCPClient,
    MCPTransport,
    SseTransport,
    StdioTransport,
)


def _run(coro):
    return asyncio.run(coro)


class TestMCPTransportABC:
    """MCPTransport 抽象基类。"""

    def test_cannot_instantiate(self) -> None:
        """抽象基类不能直接实例化。"""
        with pytest.raises(TypeError):
            MCPTransport()

    def test_abstract_methods(self) -> None:
        """有抽象方法。"""
        assert hasattr(MCPTransport, "connect")
        assert hasattr(MCPTransport, "send_request")
        assert hasattr(MCPTransport, "disconnect")
        assert hasattr(MCPTransport, "is_connected")


class TestStdioTransport:
    """StdioTransport。"""

    def test_init(self) -> None:
        """初始化。"""
        t = StdioTransport("npx", ["-y", "server"], timeout=10)
        assert not t.is_connected

    @patch("asyncio.create_subprocess_exec")
    def test_connect(self, mock_exec) -> None:
        """连接成功。"""
        mock_process = MagicMock()
        mock_exec.return_value = mock_process

        t = StdioTransport("test")
        _run(t.connect())

        assert t.is_connected
        mock_exec.assert_called_once()

    @patch("asyncio.create_subprocess_exec")
    def test_double_connect_raises(self, mock_exec) -> None:
        """重复连接报错。"""
        mock_process = MagicMock()
        mock_exec.return_value = mock_process

        t = StdioTransport("test")
        _run(t.connect())

        with pytest.raises(RuntimeError, match="already connected"):
            _run(t.connect())

    @pytest.mark.asyncio
    async def test_send_request_not_connected(self) -> None:
        """未连接时发送报错。"""
        t = StdioTransport("test")
        with pytest.raises(RuntimeError, match="not connected"):
            await t.send_request({"jsonrpc": "2.0"})

    @pytest.mark.asyncio
    async def test_send_request_eof_raises_connection_error(self) -> None:
        """EOF 时抛 ConnectionError。"""
        t = StdioTransport("test")
        t._process = MagicMock()
        t._process.stdin = MagicMock()
        t._process.stdin.write = MagicMock()
        t._process.stdin.drain = AsyncMock()
        t._process.stdout = MagicMock()
        t._process.stdout.readline = AsyncMock(return_value=b"")

        with pytest.raises(ConnectionError, match="closed connection"):
            await t.send_request({"jsonrpc": "2.0"})

    @pytest.mark.asyncio
    async def test_disconnect_not_connected(self) -> None:
        """未连接时 disconnect 不报错。"""
        t = StdioTransport("test")
        await t.disconnect()  # 不应抛异常


class TestSseTransport:
    """SseTransport。"""

    def test_init(self) -> None:
        """初始化。"""
        t = SseTransport("https://example.com/mcp", timeout=10)
        assert not t.is_connected

    def test_connect(self) -> None:
        """连接（简化版只标记）。"""
        t = SseTransport("https://example.com/mcp")
        _run(t.connect())
        assert t.is_connected

    def test_disconnect(self) -> None:
        """断开连接。"""
        t = SseTransport("https://example.com/mcp")
        _run(t.connect())
        _run(t.disconnect())
        assert not t.is_connected

    @pytest.mark.asyncio
    async def test_send_request_not_connected(self) -> None:
        """未连接时发送报错。"""
        t = SseTransport("https://example.com/mcp")
        with pytest.raises(RuntimeError, match="not connected"):
            await t.send_request({"jsonrpc": "2.0"})

    @pytest.mark.asyncio
    @patch("urllib.request.urlopen")
    async def test_send_request_json_response(self, mock_urlopen) -> None:
        """纯 JSON 响应解析。"""
        mock_resp = MagicMock()
        mock_resp.read = MagicMock(return_value=json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}).encode())
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        t = SseTransport("https://example.com/mcp")
        await t.connect()
        result = await t.send_request({"jsonrpc": "2.0", "id": 1, "method": "test"})

        assert result["jsonrpc"] == "2.0"

    @pytest.mark.asyncio
    @patch("urllib.request.urlopen")
    async def test_send_request_sse_response(self, mock_urlopen) -> None:
        """SSE 格式响应解析（data: {...}）。"""
        sse_response = "event: message\ndata: {\"jsonrpc\": \"2.0\", \"id\": 1, \"result\": {\"ok\": true}}\n\n"
        mock_resp = MagicMock()
        mock_resp.read = MagicMock(return_value=sse_response.encode())
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        t = SseTransport("https://example.com/mcp")
        await t.connect()
        result = await t.send_request({"jsonrpc": "2.0"})

        assert result["result"]["ok"] is True

    @pytest.mark.asyncio
    @patch("urllib.request.urlopen")
    async def test_send_request_http_error(self, mock_urlopen) -> None:
        """HTTP 错误抛 ConnectionError。"""
        import urllib.error
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "https://example.com/mcp", 500, "Internal Server Error", {}, None
        )

        t = SseTransport("https://example.com/mcp")
        await t.connect()

        with pytest.raises(ConnectionError, match="HTTP error 500"):
            await t.send_request({"jsonrpc": "2.0"})


class TestMCPClientTransport:
    """MCPClient 传输层参数。"""

    def test_default_stdio_transport(self) -> None:
        """默认使用 StdioTransport。"""
        client = MCPClient("npx", ["-y", "server"])
        assert isinstance(client.transport, StdioTransport)

    def test_custom_transport(self) -> None:
        """传入自定义传输。"""
        t = SseTransport("https://example.com/mcp")
        client = MCPClient(transport=t)
        assert client.transport is t

    def test_neither_command_nor_transport_raises(self) -> None:
        """command 和 transport 都没有时报错。"""
        with pytest.raises(ValueError, match="Either"):
            MCPClient()

    def test_sse_transport_client(self) -> None:
        """使用 SSE 传输创建客户端。"""
        t = SseTransport("https://example.com/mcp")
        client = MCPClient(transport=t, server_name="remote")
        assert client.server_name == "remote"
        assert isinstance(client.transport, SseTransport)


class TestAutoReconnect:
    """自动重连。"""

    @pytest.mark.asyncio
    async def test_reconnect_on_connection_error(self) -> None:
        """连接错误时自动重连。"""
        transport = MagicMock()
        transport.is_connected = True
        # 第一次失败，第二次成功
        transport.send_request = AsyncMock(side_effect=[
            ConnectionError("Broken pipe"),
            {"jsonrpc": "2.0", "id": 2, "result": {"ok": True}},  # initialize 响应
            {"jsonrpc": "2.0", "id": 3, "result": {"data": "reconnected"}},
        ])
        transport.disconnect = AsyncMock()
        transport.connect = AsyncMock()

        client = MCPClient(transport=transport, max_retries=1)
        client._initialized = True

        # mock asyncio.sleep 避免等待
        with patch("asyncio.sleep", new=AsyncMock()):
            result = await client._send_request({"jsonrpc": "2.0", "id": 1, "method": "test"})

        assert result["result"]["data"] == "reconnected"
        # 验证重连发生了
        assert transport.disconnect.called
        assert transport.connect.called

    @pytest.mark.asyncio
    async def test_reconnect_exhausted_raises(self) -> None:
        """重连次数耗尽后抛 ConnectionError。"""
        transport = MagicMock()
        transport.is_connected = True
        transport.send_request = AsyncMock(side_effect=ConnectionError("Always fails"))
        transport.disconnect = AsyncMock()
        transport.connect = AsyncMock()

        client = MCPClient(transport=transport, max_retries=1)
        client._initialized = True

        with patch("asyncio.sleep", new=AsyncMock()):
            with pytest.raises(ConnectionError, match="failed after"):
                await client._send_request({"jsonrpc": "2.0"})

    @pytest.mark.asyncio
    async def test_no_reconnect_on_non_connection_error(self) -> None:
        """非连接错误不触发重连。"""
        transport = MagicMock()
        transport.is_connected = True
        transport.send_request = AsyncMock(side_effect=RuntimeError("Other error"))

        client = MCPClient(transport=transport, max_retries=3)
        client._initialized = True

        with pytest.raises(RuntimeError, match="Other error"):
            await client._send_request({"jsonrpc": "2.0"})

        # 不应触发重连
        assert not transport.disconnect.called

    @pytest.mark.asyncio
    async def test_not_connected_raises(self) -> None:
        """未连接时 _send_request 报错。"""
        transport = MagicMock()
        transport.is_connected = False
        client = MCPClient(transport=transport)

        with pytest.raises(RuntimeError, match="not connected"):
            await client._send_request({"jsonrpc": "2.0"})
