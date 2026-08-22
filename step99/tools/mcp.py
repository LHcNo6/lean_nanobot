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
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from typing import Any

from step99.schema import StringSchema, tool_parameters_schema
from step99.tool import Tool, ToolResult, tool_parameters


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
# 传输层抽象（step88）
# ---------------------------------------------------------------------------


class MCPTransport(ABC):
    """MCP 传输层抽象基类。

    定义 MCP 客户端与服务器之间的通信接口，支持 stdio/SSE 等多种传输方式。
    """

    @abstractmethod
    async def connect(self) -> None:
        """建立连接。"""
        raise NotImplementedError

    @abstractmethod
    async def send_request(self, request: dict[str, Any]) -> dict[str, Any]:
        """发送 JSON-RPC 请求并等待响应。

        Args:
            request: JSON-RPC 请求字典。

        Returns:
            响应字典。
        """
        raise NotImplementedError

    @abstractmethod
    async def disconnect(self) -> None:
        """关闭连接。"""
        raise NotImplementedError

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """是否已连接。"""
        raise NotImplementedError


class StdioTransport(MCPTransport):
    """stdio 子进程传输。

    通过 asyncio.create_subprocess_exec 启动子进程，使用 stdin/stdout 通信。
    这是 MCP 最常用的本地传输方式。
    """

    def __init__(
        self,
        command: str,
        args: list[str] | None = None,
        timeout: int = 30,
    ):
        """初始化 stdio 传输。

        Args:
            command: 启动 MCP server 的命令。
            args: 命令参数。
            timeout: 请求超时秒数。
        """
        self._command = command
        self._args = args or []
        self._timeout = timeout
        self._process: asyncio.subprocess.Process | None = None

    @property
    def is_connected(self) -> bool:
        """是否已连接。"""
        return self._process is not None

    async def connect(self) -> None:
        """启动子进程。"""
        if self._process is not None:
            raise RuntimeError("StdioTransport is already connected.")

        self._process = await asyncio.create_subprocess_exec(
            self._command,
            *self._args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    async def send_request(self, request: dict[str, Any]) -> dict[str, Any]:
        """通过 stdin 发送请求，从 stdout 读取响应。

        Args:
            request: JSON-RPC 请求字典。

        Returns:
            响应字典。

        Raises:
            RuntimeError: 未连接。
            ConnectionError: 连接断开。
            asyncio.TimeoutError: 超时。
        """
        if self._process is None or self._process.stdin is None or self._process.stdout is None:
            raise RuntimeError("StdioTransport is not connected.")

        payload = json.dumps(request) + "\n"
        self._process.stdin.write(payload.encode("utf-8"))
        await self._process.stdin.drain()

        try:
            line = await asyncio.wait_for(
                self._process.stdout.readline(),
                timeout=self._timeout,
            )
        except asyncio.TimeoutError:
            raise asyncio.TimeoutError(f"MCP request timed out after {self._timeout}s")

        if not line:
            raise ConnectionError("MCP server closed connection (EOF).")

        try:
            return json.loads(line.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Invalid JSON-RPC response: {exc}")

    async def disconnect(self) -> None:
        """终止子进程。"""
        if self._process is None:
            return

        try:
            self._process.terminate()
            await asyncio.wait_for(self._process.wait(), timeout=5)
        except (ProcessLookupError, asyncio.TimeoutError):
            self._process.kill()
        finally:
            self._process = None


class SseTransport(MCPTransport):
    """SSE HTTP 传输（简化版）。

    通过 HTTP POST 发送 JSON-RPC 请求，解析 JSON 响应。
    简化版：每次请求独立 HTTP 调用，不保持长连接。
    适用于远程 MCP server（如 https://example.com/mcp）。
    """

    def __init__(
        self,
        url: str,
        timeout: int = 30,
        headers: dict[str, str] | None = None,
    ):
        """初始化 SSE 传输。

        Args:
            url: MCP server 的 HTTP 端点。
            timeout: 请求超时秒数。
            headers: 自定义 HTTP 头。
        """
        self._url = url
        self._timeout = timeout
        self._headers = headers or {}
        self._connected = False

    @property
    def is_connected(self) -> bool:
        """是否已连接。"""
        return self._connected

    async def connect(self) -> None:
        """建立连接（简化版：只标记已连接，实际请求时建立 HTTP 连接）。"""
        self._connected = True

    async def send_request(self, request: dict[str, Any]) -> dict[str, Any]:
        """通过 HTTP POST 发送请求，解析 JSON 响应。

        Args:
            request: JSON-RPC 请求字典。

        Returns:
            响应字典。

        Raises:
            RuntimeError: 未连接。
            ConnectionError: 网络错误。
        """
        if not self._connected:
            raise RuntimeError("SseTransport is not connected.")

        payload = json.dumps(request).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **self._headers,
        }

        req = urllib.request.Request(self._url, data=payload, headers=headers, method="POST")

        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raise ConnectionError(f"MCP HTTP error {exc.code}: {exc.reason}")
        except urllib.error.URLError as exc:
            raise ConnectionError(f"MCP connection failed: {exc.reason}")

        # 解析响应（可能是纯 JSON 或 SSE 格式）
        text = raw.decode("utf-8", errors="replace")

        # 尝试直接解析 JSON
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # 尝试解析 SSE 格式（data: {...}）
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                data = line[5:].strip()
                if data:
                    try:
                        return json.loads(data)
                    except json.JSONDecodeError:
                        continue

        raise RuntimeError("Failed to parse MCP SSE response.")

    async def disconnect(self) -> None:
        """关闭连接（简化版：只标记断开）。"""
        self._connected = False


# ---------------------------------------------------------------------------
# MCPClient
# ---------------------------------------------------------------------------


class MCPClient:
    """MCP 客户端：传输层 + JSON-RPC 协议 + 自动重连。

    step88 重构：引入 MCPTransport 抽象层，支持 stdio/SSE 传输，
    并在连接断开时自动重连（指数退避，最多 max_retries 次）。
    """

    def __init__(
        self,
        command: str | None = None,
        args: list[str] | None = None,
        timeout: int = 30,
        server_name: str = "default",
        transport: MCPTransport | None = None,
        max_retries: int = 3,
    ):
        """初始化 MCP 客户端。

        Args:
            command: 启动 MCP server 的命令（stdio 传输时使用）。
            args: 命令参数。
            timeout: 超时秒数。
            server_name: server 名称（用于工具名前缀）。
            transport: 自定义传输层（传入时忽略 command/args）。
            max_retries: 自动重连最大次数（默认3）。
        """
        self._timeout = timeout
        self._server_name = server_name
        self._request_id = 0
        self._initialized = False
        self._max_retries = max_retries

        # 传输层：传入则使用，否则默认 stdio
        if transport is not None:
            self._transport = transport
        elif command is not None:
            self._transport = StdioTransport(command, args, timeout)
        else:
            raise ValueError("Either 'command' or 'transport' must be provided.")

    @property
    def server_name(self) -> str:
        """server 名称。"""
        return self._server_name

    @property
    def is_connected(self) -> bool:
        """是否已连接并初始化。"""
        return self._transport.is_connected and self._initialized

    @property
    def transport(self) -> MCPTransport:
        """当前传输层。"""
        return self._transport

    def _next_id(self) -> int:
        """生成下一个请求 ID。"""
        self._request_id += 1
        return self._request_id

    async def connect(self) -> None:
        """建立连接并发送 initialize 请求。

        Raises:
            RuntimeError: 已连接。
        """
        if self._transport.is_connected:
            raise RuntimeError("MCPClient is already connected.")

        await self._transport.connect()

        # 发送 initialize
        init_req = _jsonrpc_request("initialize", {
            "protocolVersion": _MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "learn_nano", "version": "1.0"},
        }, request_id=self._next_id())

        await self._transport.send_request(init_req)
        self._initialized = True

    async def disconnect(self) -> None:
        """关闭连接。"""
        try:
            await self._transport.disconnect()
        finally:
            self._initialized = False

    async def _send_request(self, request: dict) -> dict:
        """发送 JSON-RPC 请求，支持自动重连。

        连接断开时自动重连（指数退避），最多重试 max_retries 次。

        Args:
            request: JSON-RPC 请求字典。

        Returns:
            响应字典。

        Raises:
            RuntimeError: 未连接。
            ConnectionError: 重连失败。
        """
        if not self._transport.is_connected:
            raise RuntimeError("MCPClient is not connected.")

        last_error: Exception | None = None

        for attempt in range(self._max_retries + 1):
            try:
                return await self._transport.send_request(request)
            except (ConnectionError, EOFError, BrokenPipeError) as exc:
                last_error = exc
                if attempt >= self._max_retries:
                    break

                # 指数退避重连
                wait_time = 2 ** attempt  # 1s, 2s, 4s
                await asyncio.sleep(wait_time)

                try:
                    # 重新连接并重新 initialize
                    await self._transport.disconnect()
                    await self._transport.connect()
                    init_req = _jsonrpc_request("initialize", {
                        "protocolVersion": _MCP_PROTOCOL_VERSION,
                        "capabilities": {},
                        "clientInfo": {"name": "learn_nano", "version": "1.0"},
                    }, request_id=self._next_id())
                    await self._transport.send_request(init_req)
                except Exception:
                    # 重连失败，继续下一次重试
                    continue

        raise ConnectionError(
            f"MCP request failed after {self._max_retries} retries: {last_error}"
        )

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

    async def list_resources(self) -> list[dict[str, Any]]:
        """列出 MCP server 提供的资源。

        发送 ``resources/list`` 请求，返回服务器暴露的可读取资源列表。

        Returns:
            资源列表，每个资源包含 uri/name/description/mimeType。

        Raises:
            RuntimeError: 未连接。
        """
        if not self._initialized:
            raise RuntimeError("MCPClient is not initialized. Call connect() first.")

        req = _jsonrpc_request("resources/list", {}, request_id=self._next_id())
        response = await self._send_request(req)

        result = response.get("result", {})
        return result.get("resources", [])

    async def read_resource(self, uri: str) -> dict[str, Any]:
        """读取 MCP 资源内容。

        发送 ``resources/read`` 请求，返回指定 URI 的资源内容。

        Args:
            uri: 资源 URI（从 list_resources 获取）。

        Returns:
            资源内容字典，包含 contents 数组。

        Raises:
            RuntimeError: 未连接。
        """
        if not self._initialized:
            raise RuntimeError("MCPClient is not initialized. Call connect() first.")

        req = _jsonrpc_request("resources/read", {"uri": uri}, request_id=self._next_id())
        return await self._send_request(req)

    async def list_prompts(self) -> list[dict[str, Any]]:
        """列出 MCP server 提供的提示词模板。

        发送 ``prompts/list`` 请求，返回服务器暴露的可复用提示词模板。

        Returns:
            提示词列表，每个包含 name/description/arguments。

        Raises:
            RuntimeError: 未连接。
        """
        if not self._initialized:
            raise RuntimeError("MCPClient is not initialized. Call connect() first.")

        req = _jsonrpc_request("prompts/list", {}, request_id=self._next_id())
        response = await self._send_request(req)

        result = response.get("result", {})
        return result.get("prompts", [])

    async def get_prompt(self, name: str, arguments: dict | None = None) -> dict[str, Any]:
        """获取并渲染 MCP 提示词。

        发送 ``prompts/get`` 请求，返回渲染后的提示词消息。

        Args:
            name: 提示词名称（从 list_prompts 获取）。
            arguments: 提示词参数。

        Returns:
            渲染结果，包含 description 和 messages。

        Raises:
            RuntimeError: 未连接。
        """
        if not self._initialized:
            raise RuntimeError("MCPClient is not initialized. Call connect() first.")

        params: dict[str, Any] = {"name": name}
        if arguments:
            params["arguments"] = arguments

        req = _jsonrpc_request("prompts/get", params, request_id=self._next_id())
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


# ---------------------------------------------------------------------------
# MCPResourceWrapper（step86）
# ---------------------------------------------------------------------------


class MCPResourceWrapper(Tool):
    """MCP 资源包装类：将 MCP server 的 resource 包装为只读 native Tool。

    MCP Resource 是服务器暴露的可读取资源（如文件、数据库查询结果等）。
    每个 resource 有固定的 URI，包装为无参数的只读工具。
    """

    _scopes = {"core"}
    _plugin_discoverable = False  # MCP 资源需要手动注册，不自动发现

    def __init__(
        self,
        client: MCPClient,
        resource_info: dict[str, Any],
        server_name: str = "default",
        timeout: int = 30,
    ):
        """初始化 MCP 资源包装。

        Args:
            client: MCPClient 实例。
            resource_info: 资源描述（来自 resources/list）。
            server_name: server 名称。
            timeout: 读取超时秒数。
        """
        self._client = client
        self._uri = resource_info.get("uri", "")
        self._resource_name = resource_info.get("name", self._uri)
        self._description = resource_info.get("description", "")
        self._mime_type = resource_info.get("mimeType", "text/plain")
        self._server_name = server_name
        self._timeout = timeout

    @property
    def name(self) -> str:
        """工具名：`mcp_{server}_resource_{name}`。"""
        return f"mcp_{self._server_name}_resource_{_sanitize_tool_name(self._resource_name)}"

    @property
    def description(self) -> str:
        """工具描述（包含 URI）。"""
        base = self._description or self._resource_name
        return f"[MCP Resource] {base}\nURI: {self._uri}"

    @property
    def read_only(self) -> bool:
        """资源是只读的。"""
        return True

    @property
    def parameters(self) -> dict[str, Any]:
        """资源工具无参数（URI 固定）。"""
        return {"type": "object", "properties": {}, "required": []}

    @property
    def uri(self) -> str:
        """资源 URI。"""
        return self._uri

    def to_schema(self) -> dict[str, Any]:
        """转换为工具 schema。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    async def execute(self, **kwargs: Any) -> str | ToolResult:
        """读取资源内容。

        Args:
            **kwargs: 忽略（资源工具无参数）。

        Returns:
            资源文本内容，或错误。
        """
        try:
            response = await asyncio.wait_for(
                self._client.read_resource(self._uri),
                timeout=self._timeout,
            )
        except asyncio.TimeoutError:
            return ToolResult.error(
                f"Error: MCP resource '{self._resource_name}' timed out after {self._timeout}s."
            )
        except RuntimeError as exc:
            return ToolResult.error(f"Error: MCP resource read failed: {exc}")
        except Exception as exc:
            return ToolResult.error(f"Error: MCP resource error: {exc}")

        # 检查 JSON-RPC 错误
        if "error" in response:
            error = response["error"]
            return ToolResult.error(
                f"MCP error {error.get('code', '?')}: {error.get('message', 'Unknown error')}"
            )

        # 提取内容
        result = response.get("result", {})
        contents = result.get("contents", [])

        texts = []
        for item in contents:
            if isinstance(item, dict):
                if "text" in item:
                    texts.append(item["text"])
                elif "blob" in item:
                    # base64 编码的二进制内容，标注为二进制
                    texts.append(f"(binary content: {item.get('mimeType', 'application/octet-stream')})")

        if texts:
            return "\n".join(texts)

        return json.dumps(result, ensure_ascii=False, indent=2)


async def create_mcp_resources(client: MCPClient) -> list[MCPResourceWrapper]:
    """连接 MCP server 并创建所有资源的包装。

    Args:
        client: MCPClient 实例（未连接）。

    Returns:
        MCPResourceWrapper 列表。
    """
    if not client.is_connected:
        await client.connect()

    resources_info = await client.list_resources()
    return [
        MCPResourceWrapper(client=client, resource_info=info, server_name=client.server_name)
        for info in resources_info
    ]


# ---------------------------------------------------------------------------
# MCPPromptWrapper（step87）
# ---------------------------------------------------------------------------


class MCPPromptWrapper(Tool):
    """MCP 提示词包装类：将 MCP server 的 prompt 模板包装为 native Tool。

    MCP Prompt 是服务器暴露的可复用提示词模板，支持参数填充。
    调用时发送 prompts/get，服务器返回渲染后的消息。
    """

    _scopes = {"core"}
    _plugin_discoverable = False  # MCP 提示词需要手动注册，不自动发现

    def __init__(
        self,
        client: MCPClient,
        prompt_info: dict[str, Any],
        server_name: str = "default",
        timeout: int = 30,
    ):
        """初始化 MCP 提示词包装。

        Args:
            client: MCPClient 实例。
            prompt_info: 提示词描述（来自 prompts/list）。
            server_name: server 名称。
            timeout: 请求超时秒数。
        """
        self._client = client
        self._prompt_name = prompt_info.get("name", "unknown")
        self._description = prompt_info.get("description", "")
        self._arguments = prompt_info.get("arguments", [])
        self._server_name = server_name
        self._timeout = timeout
        self._parameters = self._build_parameters()

    def _build_parameters(self) -> dict[str, Any]:
        """从 prompt 的 arguments 定义构建 JSON Schema。

        Returns:
            JSON Schema 字典。
        """
        properties: dict[str, Any] = {}
        required: list[str] = []

        for arg in self._arguments:
            arg_name = arg.get("name", "")
            if not arg_name:
                continue
            properties[arg_name] = {
                "type": "string",
                "description": arg.get("description", ""),
            }
            if arg.get("required", False):
                required.append(arg_name)

        return {
            "type": "object",
            "properties": properties,
            "required": required,
        }

    @property
    def name(self) -> str:
        """工具名：`mcp_{server}_prompt_{name}`。"""
        return f"mcp_{self._server_name}_prompt_{_sanitize_tool_name(self._prompt_name)}"

    @property
    def description(self) -> str:
        """工具描述。"""
        return self._description or f"MCP prompt: {self._prompt_name}"

    @property
    def read_only(self) -> bool:
        """提示词工具是只读的。"""
        return True

    @property
    def parameters(self) -> dict[str, Any]:
        """工具参数 schema（动态生成）。"""
        return self._parameters

    @property
    def prompt_name(self) -> str:
        """MCP server 中的原始提示词名。"""
        return self._prompt_name

    def to_schema(self) -> dict[str, Any]:
        """转换为工具 schema。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    async def execute(self, **kwargs: Any) -> str | ToolResult:
        """获取并渲染提示词。

        Args:
            **kwargs: 提示词参数。

        Returns:
            渲染后的提示词消息文本，或错误。
        """
        try:
            response = await asyncio.wait_for(
                self._client.get_prompt(self._prompt_name, kwargs),
                timeout=self._timeout,
            )
        except asyncio.TimeoutError:
            return ToolResult.error(
                f"Error: MCP prompt '{self._prompt_name}' timed out after {self._timeout}s."
            )
        except RuntimeError as exc:
            return ToolResult.error(f"Error: MCP prompt failed: {exc}")
        except Exception as exc:
            return ToolResult.error(f"Error: MCP prompt error: {exc}")

        # 检查 JSON-RPC 错误
        if "error" in response:
            error = response["error"]
            return ToolResult.error(
                f"MCP error {error.get('code', '?')}: {error.get('message', 'Unknown error')}"
            )

        # 提取消息内容
        result = response.get("result", {})
        messages = result.get("messages", [])

        texts = []
        for msg in messages:
            if isinstance(msg, dict):
                role = msg.get("role", "user")
                content = msg.get("content", "")
                if isinstance(content, list):
                    # content 是数组，提取 text 类型
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "text":
                            texts.append(f"[{role}] {item.get('text', '')}")
                elif isinstance(content, dict):
                    # content 是单个内容块 dict
                    if content.get("type") == "text":
                        texts.append(f"[{role}] {content.get('text', '')}")
                elif isinstance(content, str):
                    texts.append(f"[{role}] {content}")

        if texts:
            return "\n\n".join(texts)

        return json.dumps(result, ensure_ascii=False, indent=2)


async def create_mcp_prompts(client: MCPClient) -> list[MCPPromptWrapper]:
    """连接 MCP server 并创建所有提示词的包装。

    Args:
        client: MCPClient 实例（未连接）。

    Returns:
        MCPPromptWrapper 列表。
    """
    if not client.is_connected:
        await client.connect()

    prompts_info = await client.list_prompts()
    return [
        MCPPromptWrapper(client=client, prompt_info=info, server_name=client.server_name)
        for info in prompts_info
    ]
