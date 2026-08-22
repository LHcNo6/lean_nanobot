"""step86：MCP Resource 支持单元测试。"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from step100.tools.mcp import (
    MCPClient,
    MCPResourceWrapper,
    create_mcp_resources,
)


def _run(coro):
    return asyncio.run(coro)


def _make_mock_client(initialized: bool = True) -> MCPClient:
    """创建一个 mock 的 MCPClient。"""
    client = MCPClient("test", server_name="test")
    if initialized:
        client._initialized = True
        client._process = MagicMock()
    return client


class TestMCPClientResources:
    """MCPClient resource 方法。"""

    def test_list_resources_not_initialized(self) -> None:
        """未初始化时 list_resources 报错。"""
        client = _make_mock_client(initialized=False)
        with pytest.raises(RuntimeError, match="not initialized"):
            _run(client.list_resources())

    def test_read_resource_not_initialized(self) -> None:
        """未初始化时 read_resource 报错。"""
        client = _make_mock_client(initialized=False)
        with pytest.raises(RuntimeError, match="not initialized"):
            _run(client.read_resource("file:///test"))

    @pytest.mark.asyncio
    async def test_list_resources_request(self) -> None:
        """list_resources 发送正确的 JSON-RPC 请求。"""
        client = _make_mock_client()
        client._send_request = AsyncMock(return_value={
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "resources": [
                    {"uri": "file:///a.txt", "name": "a", "description": "File A", "mimeType": "text/plain"},
                    {"uri": "file:///b.txt", "name": "b", "description": "File B", "mimeType": "text/plain"},
                ]
            },
        })

        resources = await client.list_resources()

        assert len(resources) == 2
        assert resources[0]["uri"] == "file:///a.txt"
        assert resources[1]["name"] == "b"
        # 验证发送的请求
        call_args = client._send_request.call_args[0][0]
        assert call_args["method"] == "resources/list"

    @pytest.mark.asyncio
    async def test_read_resource_request(self) -> None:
        """read_resource 发送正确的 JSON-RPC 请求。"""
        client = _make_mock_client()
        client._send_request = AsyncMock(return_value={
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "contents": [
                    {"uri": "file:///a.txt", "mimeType": "text/plain", "text": "Hello World"},
                ]
            },
        })

        result = await client.read_resource("file:///a.txt")

        assert result["result"]["contents"][0]["text"] == "Hello World"
        call_args = client._send_request.call_args[0][0]
        assert call_args["method"] == "resources/read"
        assert call_args["params"]["uri"] == "file:///a.txt"


class TestMCPResourceWrapper:
    """MCPResourceWrapper。"""

    def test_name_format(self) -> None:
        """工具名格式。"""
        client = _make_mock_client()
        info = {"uri": "file:///config", "name": "config_file", "description": "Config"}
        wrapper = MCPResourceWrapper(client, info, server_name="myserver")

        assert wrapper.name == "mcp_myserver_resource_config_file"

    def test_name_sanitize(self) -> None:
        """工具名清理特殊字符。"""
        client = _make_mock_client()
        info = {"uri": "file:///test", "name": "my resource!", "description": "Test"}
        wrapper = MCPResourceWrapper(client, info, server_name="srv")

        assert wrapper.name == "mcp_srv_resource_my_resource"

    def test_description_contains_uri(self) -> None:
        """描述包含 URI。"""
        client = _make_mock_client()
        info = {"uri": "file:///data.json", "name": "data", "description": "Data file"}
        wrapper = MCPResourceWrapper(client, info)

        assert "Data file" in wrapper.description
        assert "file:///data.json" in wrapper.description

    def test_default_description(self) -> None:
        """无 description 时用 name。"""
        client = _make_mock_client()
        info = {"uri": "file:///x", "name": "x_resource"}
        wrapper = MCPResourceWrapper(client, info)

        assert "x_resource" in wrapper.description

    def test_read_only(self) -> None:
        """资源是只读的。"""
        client = _make_mock_client()
        wrapper = MCPResourceWrapper(client, {"uri": "x", "name": "x"})
        assert wrapper.read_only is True

    def test_no_parameters(self) -> None:
        """资源工具无参数。"""
        client = _make_mock_client()
        wrapper = MCPResourceWrapper(client, {"uri": "x", "name": "x"})
        params = wrapper.parameters
        assert params["type"] == "object"
        assert params["properties"] == {}
        assert params["required"] == []

    def test_not_auto_discoverable(self) -> None:
        """不自动发现。"""
        assert MCPResourceWrapper._plugin_discoverable is False

    def test_uri_property(self) -> None:
        """uri 属性。"""
        client = _make_mock_client()
        wrapper = MCPResourceWrapper(client, {"uri": "file:///test", "name": "t"})
        assert wrapper.uri == "file:///test"

    def test_to_schema(self) -> None:
        """to_schema 格式。"""
        client = _make_mock_client()
        wrapper = MCPResourceWrapper(client, {"uri": "x", "name": "res", "description": "R"}, server_name="s")
        schema = wrapper.to_schema()

        assert schema["type"] == "function"
        assert schema["function"]["name"] == "mcp_s_resource_res"
        assert schema["function"]["parameters"]["properties"] == {}

    @pytest.mark.asyncio
    async def test_execute_success(self) -> None:
        """执行成功返回文本。"""
        client = _make_mock_client()
        client.read_resource = AsyncMock(return_value={
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "contents": [
                    {"uri": "file:///x", "mimeType": "text/plain", "text": "Resource content here"},
                ]
            },
        })
        wrapper = MCPResourceWrapper(client, {"uri": "file:///x", "name": "x"})

        result = await wrapper.execute()
        assert "Resource content here" in str(result)

    @pytest.mark.asyncio
    async def test_execute_jsonrpc_error(self) -> None:
        """JSON-RPC 错误返回 ToolResult.error。"""
        from step100.tool import ToolResult
        client = _make_mock_client()
        client.read_resource = AsyncMock(return_value={
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32000, "message": "Resource not found"},
        })
        wrapper = MCPResourceWrapper(client, {"uri": "file:///x", "name": "x"})

        result = await wrapper.execute()
        assert isinstance(result, ToolResult)
        assert result.is_error
        assert "Resource not found" in str(result)

    @pytest.mark.asyncio
    async def test_execute_binary_content(self) -> None:
        """二进制内容标注。"""
        client = _make_mock_client()
        client.read_resource = AsyncMock(return_value={
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "contents": [
                    {"uri": "file:///img.png", "mimeType": "image/png", "blob": "base64data"},
                ]
            },
        })
        wrapper = MCPResourceWrapper(client, {"uri": "file:///img.png", "name": "img"})

        result = await wrapper.execute()
        assert "binary content" in str(result)
        assert "image/png" in str(result)


class TestCreateMcpResources:
    """create_mcp_resources 辅助函数。"""

    @patch("asyncio.create_subprocess_exec")
    def test_create_resources(self, mock_exec) -> None:
        """从 MCP server 创建资源包装列表。"""
        mock_process = MagicMock()
        mock_process.stdin = MagicMock()
        mock_process.stdin.write = MagicMock()
        mock_process.stdin.drain = AsyncMock()
        mock_process.stdout = MagicMock()
        responses = [
            json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}).encode(),  # initialize
            json.dumps({
                "jsonrpc": "2.0", "id": 2,
                "result": {"resources": [
                    {"uri": "file:///a", "name": "res_a", "description": "A"},
                    {"uri": "file:///b", "name": "res_b", "description": "B"},
                ]},
            }).encode(),
        ]
        mock_process.stdout.readline = AsyncMock(side_effect=responses)
        mock_process.terminate = MagicMock()
        mock_process.wait = AsyncMock(return_value=0)
        mock_exec.return_value = mock_process

        client = MCPClient("test", server_name="myserver")
        wrappers = _run(create_mcp_resources(client))

        assert len(wrappers) == 2
        assert wrappers[0].name == "mcp_myserver_resource_res_a"
        assert wrappers[1].name == "mcp_myserver_resource_res_b"
        assert wrappers[0].uri == "file:///a"
