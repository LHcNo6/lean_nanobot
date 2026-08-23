# Step 82 Design: MCP 协议基础框架

## 1. 架构

```
tools/mcp.py（新建）
  ├── MCPClient          MCP 客户端（stdio 传输 + JSON-RPC）
  ├── MCPTool(Tool)      MCP 工具包装类
  └── _jsonrpc_request   JSON-RPC 请求构造
```

## 2. MCPClient

```python
class MCPClient:
    def __init__(self, command: str, args: list[str] = None)
    async def connect(self) -> None      # 启动子进程，发送 initialize
    async def disconnect(self) -> None   # 关闭连接
    async def list_tools(self) -> list[dict]  # tools/list
    async def call_tool(self, name: str, args: dict) -> dict  # tools/call
```

## 3. JSON-RPC 协议

请求格式：
```json
{"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
```

响应格式：
```json
{"jsonrpc": "2.0", "id": 1, "result": {...}}
```

## 4. MCPTool

将 MCP server 返回的工具描述包装为 native Tool：
- name: mcp_{server}_{tool_name}
- description: 来自 MCP server
- execute: 调用 MCPClient.call_tool

## 5. 测试策略

- JSON-RPC 请求构造
- MCPClient 初始化
- MCPTool 包装
- 工具名 sanitize
- 用 mock subprocess 测试连接流程
