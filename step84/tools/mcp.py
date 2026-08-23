"""MCP 协议基础框架（step82）。

对齐 nanobot `agent/tools/mcp.py` 的最小子集：
- MCPClient：stdio 传输 + JSON-RPC 协议；
- MCPTool：将 MCP server 工具包装为 native Tool；
- 支持 initialize / tools/list / tools/call。

简化版：不依赖外部 mcp SDK，用 asyncio subprocess 实现 stdio 传输。
不实现 SSE/HTTP 传输、自动重连、多 server 管理。
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from step84.schema import StringSchema, tool_parameters_schema
from step84.tool import Tool, ToolResult, tool_parameters


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# MCP 工具名允许的字符（替换非法字符为下划线）
_SANITIZE_RE = re.compile(r"[^a-zA-Z0-9_-]")
_COLLAPSE_RE = re.compile(r"_+")

_MCP_PROTOCOL_VERSION = "2024-11-05"


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _sanitize_tool_name(name: str) -> str:
    """清理工具名，使其符合模型 provider 的要求。

    替换非 [a-zA-Z0-9_-] 字符为下划线，合并连续下划线。

    Args:
        name: 原始工具名。

    Returns:
        清理后的工具名。
    """
    cleaned = _SANITIZE_RE.sub("_", name)
    cleaned = _COLLAPSE_RE.sub("_", cleaned)
    return cleaned.strip("_") or "mcp_tool"


def _jsonrpc_request(method: str, params: dict | None = None, request_id: int = 1) -> dict:
    """构造 JSON-RPC 请求。

    Args:
        method: 方法名。
        params: 参数。
        request_id: 请求 ID。

    Returns:
        JSON-RPC 请求字典。
    """
    req: dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
    }
    if params is not None:
        req["params"] = params
    return req


# ---------------------------------------------------------------------------
# MCPClient
# ---------------------------------------------------------------------------


class MCPClient:
    """MCP 客户端：stdio 传输 + JSON-RPC 协议。

    通过子进程 stdio 与 MCP server 通信，支持 initialize/tools/list/tools/call。
    """

    def __init__(
        self,
        command: str,
        args: list[str] | None = None,
        timeout: int = 30,
        server_name: str = "default",
    ):
        """初始化 MCP 客户端。

        Args:
            command: 启动 MCP server 的命令。
            args: 命令参数。
            timeout: 超时秒数。
            server_name: server 名称（用于工具名前缀）。
        """
        self._command = command
        self._args = args or []
        self._timeout = timeout
        self._server_name = server_name
        self._process: asyncio.subprocess.Process | None = None
        self._request_id = 0
        self._initialized = False

    @property
    def server_name(self) -> str:
        """server 名称。"""
        return self._server_name

    @property
    def is_connected(self) -> bool:
        """是否已连接。"""
        return self._process is not None and self._initialized

    def _next_id(self) -> int:
        """生成下一个请求 ID。"""
        self._request_id += 1
        return self._request_id

    async def connect(self) -> None:
        """启动子进程并发送 initialize 请求。

        Raises:
            RuntimeError: 已连接。
            asyncio.TimeoutError: 连接超时。
        """
        if self._process is not None:
            raise RuntimeError("MCPClient is already connected.")

        # 启动子进程
        self._process = await asyncio.create_subprocess_exec(
            self._command,
            *self._args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # 发送 initialize
        init_req = _jsonrpc_request("initialize", {
            "protocolVersion": _MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "learn_nano", "version": "1.0"},
        }, request_id=self._next_id())

        await self._send_request(init_req)
        self._initialized = True

    async def disconnect(self) -> None:
        """关闭连接，终止子进程。"""
        if self._process is None:
            return

        try:
            # 发送 notifications/initialized（可选）
            pass
        finally:
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except (ProcessLookupError, asyncio.TimeoutError):
                self._process.kill()
            finally:
                self._process = None
                self._initialized = False

    async def _send_request(self, request: dict) -> dict:
        """发送 JSON-RPC 请求并等待响应。

        Args:
            request: JSON-RPC 请求字典。

        Returns:
            响应字典。

        Raises:
            RuntimeError: 未连接。
            asyncio.TimeoutError: 超时。
        """
        if self._process is None or self._process.stdin is None or self._process.stdout is None:
            raise RuntimeError("MCPClient is not connected.")

        # 发送请求
        payload = json.dumps(request) + "\n"
        self._process.stdin.write(payload.encode("utf-8"))
        await self._process.stdin.drain()

        # 读取响应（按行读取 JSON-RPC 响应）
        try:
            line = await asyncio.wait_for(
                self._process.stdout.readline(),
                timeout=self._timeout,
            )
        except asyncio.TimeoutError:
            raise asyncio.TimeoutError(f"MCP request timed out after {self._timeout}s")

        if not line:
            raise RuntimeError("MCP server closed connection unexpectedly.")

        try:
            return json.loads(line.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Invalid JSON-RPC response: {exc}")

    async def list_tools(self) -> list[dict[str, Any]]:
        """列出 MCP server 提供的工具。

        Returns:
            工具列表，每个工具包含 name/description/inputSchema。

        Raises:
            RuntimeError: 未连接。
        """
        if not self._initialized:
            raise RuntimeError("MCPClient is not initialized. Call connect() first.")

        req = _jsonrpc_request("tools/list", {}, request_id=self._next_id())
        response = await self._send_request(req)

        result = response.get("result", {})
        return result.get("tools", [])

    async def call_tool(self, name: str, arguments: dict | None = None) -> dict[str, Any]:
        """调用 MCP 工具。

        Args:
            name: 工具名。
            arguments: 工具参数。

        Returns:
            工具调用结果。

        Raises:
            RuntimeError: 未连接。
        """
        if not self._initialized:
            raise RuntimeError("MCPClient is not initialized. Call connect() first.")

        req = _jsonrpc_request("tools/call", {
            "name": name,
            "arguments": arguments or {},
        }, request_id=self._next_id())

        return await self._send_request(req)


# ---------------------------------------------------------------------------
# MCPTool
# ---------------------------------------------------------------------------


class MCPTool(Tool):
    """MCP 工具包装类：将 MCP server 工具包装为 native Tool。

    从 MCPClient.list_tools() 获取工具描述后，用此类包装，
    使其可以注册到 ToolRegistry 并被 agent 调用。
    """

    _scopes = {"core"}
    _plugin_discoverable = False  # MCP 工具需要手动注册，不自动发现

    def __init__(
        self,
        client: MCPClient,
        tool_info: dict[str, Any],
        server_name: str = "default",
    ):
        """初始化 MCP 工具包装。

        Args:
            client: MCPClient 实例。
            tool_info: 工具描述（来自 tools/list）。
            server_name: server 名称。
        """
        self._client = client
        self._tool_info = tool_info
        self._server_name = server_name
        self._mcp_name = tool_info.get("name", "unknown")
        self._description = tool_info.get("description", "")
        self._input_schema = tool_info.get("inputSchema", {"type": "object", "properties": {}})

    @property
    def name(self) -> str:
        """工具名：``mcp_{server}_{tool_name}``。"""
        return f"mcp_{self._server_name}_{_sanitize_tool_name(self._mcp_name)}"

    @property
    def description(self) -> str:
        """工具描述（来自 MCP server）。"""
        return self._description or f"MCP tool: {self._mcp_name}"

    @property
    def read_only(self) -> bool:
        """MCP 工具默认不是只读（可能有副作用）。"""
        return False

    @property
    def parameters(self) -> dict[str, Any]:
        """工具参数 schema（来自 MCP server 的 inputSchema）。"""
        return self._input_schema

    @property
    def mcp_name(self) -> str:
        """MCP server 中的原始工具名。"""
        return self._mcp_name

    def to_schema(self) -> dict[str, Any]:
        """转换为工具 schema（使用 MCP server 提供的 inputSchema）。

        Returns:
            工具 schema 字典。
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self._input_schema,
            },
        }

    async def execute(self, **kwargs: Any) -> str | ToolResult:
        """调用 MCP 工具。

        Args:
            **kwargs: 工具参数。

        Returns:
            工具调用结果文本，或错误。
        """
        try:
            response = await self._client.call_tool(self._mcp_name, kwargs)
        except asyncio.TimeoutError:
            return ToolResult.error(f"Error: MCP tool '{self._mcp_name}' timed out.")
        except RuntimeError as exc:
            return ToolResult.error(f"Error: MCP tool '{self._mcp_name}' failed: {exc}")
        except Exception as exc:
            return ToolResult.error(f"Error: MCP tool '{self._mcp_name}' error: {exc}")

        # 检查 JSON-RPC 错误
        if "error" in response:
            error = response["error"]
            return ToolResult.error(
                f"MCP error {error.get('code', '?')}: {error.get('message', 'Unknown error')}"
            )

        # 提取结果
        result = response.get("result", {})
        content = result.get("content", [])

        # 格式化文本内容
        texts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                texts.append(item.get("text", ""))

        if texts:
            return "\n".join(texts)

        # 没有文本内容，返回 JSON
        return json.dumps(result, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# 辅助：从 MCP server 创建工具列表
# ---------------------------------------------------------------------------


async def create_mcp_tools(client: MCPClient) -> list[MCPTool]:
    """连接 MCP server 并创建所有工具的包装。

    Args:
        client: MCPClient 实例（未连接）。

    Returns:
        MCPTool 列表。
    """
    if not client.is_connected:
        await client.connect()

    tools_info = await client.list_tools()
    return [
        MCPTool(client=client, tool_info=info, server_name=client.server_name)
        for info in tools_info
    ]
