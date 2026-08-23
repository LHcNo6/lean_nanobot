"""step82：MCP 协议基础框架单元测试。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from step124.tools.mcp import (
    MCPClient,
    MCPTool,
    _jsonrpc_request,
    _sanitize_tool_name,
    create_mcp_tools,
)


def _run(coro):
    return asyncio.run(coro)


class TestSanitizeToolName:
    """工具名清理。"""

    def test_valid_name(self) -> None:
        """合法工具名不变。"""
        assert _sanitize_tool_name("my_tool") == "my_tool"

    def test_spaces_replaced(self) -> None:
        """空格替换为下划线。"""
        assert _sanitize_tool_name("my tool") == "my_tool"

    def test_special_chars_replaced(self) -> None:
        """特殊字符替换为下划线。"""
        assert _sanitize_tool_name("my@tool!") == "my_tool"

    def test_collapse_underscores(self) -> None:
        """连续下划线合并。"""
        assert _sanitize_tool_name("my__tool") == "my_tool"

    def test_strip_leading_trailing(self) -> None:
        """去除首尾下划线。"""
        assert _sanitize_tool_name("_my_tool_") == "my_tool"

    def test_empty_name(self) -> None:
        """空名称返回默认。"""
        assert _sanitize_tool_name("") == "mcp_tool"


class TestJsonRpcRequest:
    """JSON-RPC 请求构造。"""

    def test_basic_request(self) -> None:
        """基本请求构造。"""
        req = _jsonrpc_request("tools/list", {}, request_id=1)
        assert req["jsonrpc"] == "2.0"
        assert req["id"] == 1
        assert req["method"] == "tools/list"
        assert req["params"] == {}

    def test_request_without_params(self) -> None:
        """无 params 的请求。"""
        req = _jsonrpc_request("initialize", request_id=2)
        assert req["method"] == "initialize"
        assert "params" not in req

    def test_request_with_params(self) -> None:
        """带 params 的请求。"""
        req = _jsonrpc_request("tools/call", {"name": "test"}, request_id=3)
        assert req["params"]["name"] == "test"


class TestMCPClient:
    """MCPClient。"""

    def test_init(self) -> None:
        """初始化。"""
        client = MCPClient("npx", ["-y", "@modelcontextprotocol/server-filesystem"], server_name="fs")
        assert client.server_name == "fs"
        assert not client.is_connected

    def test_not_connected_raises(self) -> None:
        """未连接时调用 list_tools 报错。"""
        client = MCPClient("test")
        with pytest.raises(RuntimeError, match="not initialized"):
            _run(client.list_tools())

    def test_double_connect_raises(self) -> None:
        """重复连接报错。"""
        client = MCPClient("test")
        client._transport = MagicMock()
        client._transport.is_connected = True
        client._initialized = True
        with pytest.raises(RuntimeError, match="already connected"):
            _run(client.connect())

    @patch("asyncio.create_subprocess_exec")
    def test_connect(self, mock_exec) -> None:
        """连接成功。"""
        # mock subprocess
        mock_process = MagicMock()
        mock_process.stdin = MagicMock()
        mock_process.stdin.write = MagicMock()
        mock_process.stdin.drain = AsyncMock()
        mock_process.stdout = MagicMock()
        mock_process.stdout.readline = AsyncMock(return_value=json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}).encode())
        mock_process.terminate = MagicMock()
        mock_process.wait = AsyncMock(return_value=0)
        mock_exec.return_value = mock_process

        client = MCPClient("test")
        _run(client.connect())

        assert client.is_connected
        mock_exec.assert_called_once()

    @patch("asyncio.create_subprocess_exec")
    def test_disconnect(self, mock_exec) -> None:
        """断开连接。"""
        mock_process = MagicMock()
        mock_process.stdin = MagicMock()
        mock_process.stdin.write = MagicMock()
        mock_process.stdin.drain = AsyncMock()
        mock_process.stdout = MagicMock()
        mock_process.stdout.readline = AsyncMock(return_value=json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}).encode())
        mock_process.terminate = MagicMock()
        mock_process.wait = AsyncMock(return_value=0)
        mock_exec.return_value = mock_process

        client = MCPClient("test")
        _run(client.connect())
        _run(client.disconnect())

        assert not client.is_connected

    @patch("asyncio.create_subprocess_exec")
    def test_list_tools(self, mock_exec) -> None:
        """列出工具。"""
        mock_process = MagicMock()
        mock_process.stdin = MagicMock()
        mock_process.stdin.write = MagicMock()
        mock_process.stdin.drain = AsyncMock()
        mock_process.stdout = MagicMock()
        # initialize 响应 + tools/list 响应
        responses = [
            json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}).encode(),
            json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"tools": [
                {"name": "read_file", "description": "Read a file", "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}}},
            ]}}).encode(),
        ]
        mock_process.stdout.readline = AsyncMock(side_effect=responses)
        mock_process.terminate = MagicMock()
        mock_process.wait = AsyncMock(return_value=0)
        mock_exec.return_value = mock_process

        client = MCPClient("test", server_name="test")
        _run(client.connect())
        tools = _run(client.list_tools())

        assert len(tools) == 1
        assert tools[0]["name"] == "read_file"


class TestMCPTool:
    """MCPTool 包装。"""

    def test_name(self) -> None:
        """工具名格式。"""
        client = MCPClient("test", server_name="fs")
        tool_info = {"name": "read_file", "description": "Read a file", "inputSchema": {"type": "object"}}
        tool = MCPTool(client, tool_info, server_name="fs")

        assert tool.name == "mcp_fs_read_file"

    def test_description(self) -> None:
        """工具描述。"""
        client = MCPClient("test")
        tool_info = {"name": "test", "description": "A test tool"}
        tool = MCPTool(client, tool_info)

        assert tool.description == "A test tool"

    def test_default_description(self) -> None:
        """默认描述。"""
        client = MCPClient("test")
        tool_info = {"name": "test"}
        tool = MCPTool(client, tool_info)

        assert "MCP tool" in tool.description

    def test_to_schema(self) -> None:
        """转换为 schema。"""
        client = MCPClient("test")
        tool_info = {
            "name": "test",
            "description": "Test",
            "inputSchema": {"type": "object", "properties": {"x": {"type": "string"}}},
        }
        tool = MCPTool(client, tool_info)
        schema = tool.to_schema()

        assert schema["type"] == "function"
        assert schema["function"]["name"] == "mcp_default_test"
        assert schema["function"]["parameters"]["properties"]["x"]["type"] == "string"

    def test_not_auto_discoverable(self) -> None:
        """MCP 工具不自动发现。"""
        assert MCPTool._plugin_discoverable is False

    @pytest.mark.asyncio
    async def test_execute_success(self) -> None:
        """调用成功。"""
        client = MCPClient("test")
        client._initialized = True
        client._transport = MagicMock()
        client._transport.is_connected = True
        client._transport.send_request = AsyncMock(
            return_value={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"content": [{"type": "text", "text": "Hello from MCP"}]},
            }
        )

        tool_info = {"name": "greet", "description": "Greet"}
        tool = MCPTool(client, tool_info)

        result = await tool.execute(name="world")
        assert "Hello from MCP" in str(result)

    @pytest.mark.asyncio
    async def test_execute_error(self) -> None:
        """调用返回错误。"""
        client = MCPClient("test")
        client._initialized = True
        client._transport = MagicMock()
        client._transport.is_connected = True
        client._transport.send_request = AsyncMock(
            return_value={
                "jsonrpc": "2.0",
                "id": 1,
                "error": {"code": -32601, "message": "Method not found"},
            }
        )

        tool_info = {"name": "bad", "description": "Bad"}
        tool = MCPTool(client, tool_info)

        result = await tool.execute()
        from step124.tool import ToolResult
        assert isinstance(result, ToolResult)
        assert result.is_error
        assert "Method not found" in str(result)


class TestCreateMcpTools:
    """create_mcp_tools 辅助函数。"""

    @patch("asyncio.create_subprocess_exec")
    def test_create_tools(self, mock_exec) -> None:
        """从 MCP server 创建工具列表。"""
        mock_process = MagicMock()
        mock_process.stdin = MagicMock()
        mock_process.stdin.write = MagicMock()
        mock_process.stdin.drain = AsyncMock()
        mock_process.stdout = MagicMock()
        responses = [
            json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}).encode(),
            json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"tools": [
                {"name": "tool1", "description": "First", "inputSchema": {"type": "object"}},
                {"name": "tool2", "description": "Second", "inputSchema": {"type": "object"}},
            ]}}).encode(),
        ]
        mock_process.stdout.readline = AsyncMock(side_effect=responses)
        mock_process.terminate = MagicMock()
        mock_process.wait = AsyncMock(return_value=0)
        mock_exec.return_value = mock_process

        client = MCPClient("test", server_name="myserver")
        tools = _run(create_mcp_tools(client))

        assert len(tools) == 2
        assert tools[0].name == "mcp_myserver_tool1"
        assert tools[1].name == "mcp_myserver_tool2"
