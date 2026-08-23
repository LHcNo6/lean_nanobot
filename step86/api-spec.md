# Step 86 API Specification

## 1. MCPClient 新增方法

### list_resources()

```python
async def list_resources(self) -> list[dict[str, Any]]
```

发送 `resources/list` 请求，返回 resource 列表。
每个 resource 包含：`uri`, `name`, `description`, `mimeType`。

### read_resource(uri)

```python
async def read_resource(self, uri: str) -> dict[str, Any]
```

发送 `resources/read` 请求，返回 resource 内容。
返回包含 `contents` 数组，每个元素含 `uri`, `mimeType`, `text`。

## 2. MCPResourceWrapper API

**继承**：`Tool`

| 属性 | 值 |
|------|-----|
| `name` | `mcp_{server}_resource_{name}` |
| `_plugin_discoverable` | `False` |
| `read_only` | `True` |
| `parameters` | `{"type": "object", "properties": {}, "required": []}` |

### 构造

```python
MCPResourceWrapper(client: MCPClient, resource_info: dict, server_name: str = "default", timeout: int = 30)
```

### execute()

无参数，调用 `client.read_resource(uri)`，返回文本内容。

## 3. create_mcp_resources()

```python
async def create_mcp_resources(client: MCPClient) -> list[MCPResourceWrapper]
```

连接 MCP server（如未连接），列出所有 resources，创建包装列表。

## 4. JSON-RPC 协议

| method | params | 说明 |
|--------|--------|------|
| `resources/list` | `{}` | 列出资源 |
| `resources/read` | `{"uri": "..."}` | 读取资源 |
