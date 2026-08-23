# Step 82 API Specification

## 1. MCPClient API

**文件**：`tools/mcp.py`

### 构造

```python
MCPClient(command: str, args: list[str] = None, timeout: int = 30)
```

### 方法

| 方法 | 说明 |
|------|------|
| `async connect()` | 启动子进程，发送 initialize |
| `async disconnect()` | 关闭连接 |
| `async list_tools() -> list[dict]` | 列出 MCP server 工具 |
| `async call_tool(name, args) -> dict` | 调用 MCP 工具 |

## 2. MCPTool API

**继承**：`Tool`

| 属性 | 值 |
|------|-----|
| `name` | `mcp_{server}_{tool_name}` |
| `_scopes` | `{"core"}` |

### 构造

```python
MCPTool(client: MCPClient, tool_info: dict, server_name: str = "default")
```

## 3. JSON-RPC 协议

请求：`{"jsonrpc": "2.0", "id": N, "method": "...", "params": {...}}`
响应：`{"jsonrpc": "2.0", "id": N, "result": {...}}`

支持的 method：
- `initialize`
- `tools/list`
- `tools/call`

## 4. 工具发现契约

MCP 工具不通过 ToolLoader 自动发现，需要手动连接 MCP server 后注册。
