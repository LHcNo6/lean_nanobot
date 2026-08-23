# Step 87 API Specification

## 1. MCPClient 新增方法

### list_prompts()

```python
async def list_prompts(self) -> list[dict[str, Any]]
```

发送 `prompts/list` 请求，返回 prompt 模板列表。
每个 prompt 包含：`name`, `description`, `arguments`。

### get_prompt(name, arguments)

```python
async def get_prompt(self, name: str, arguments: dict | None = None) -> dict[str, Any]
```

发送 `prompts/get` 请求，返回渲染后的 prompt 消息。
返回包含 `description` 和 `messages` 数组。

## 2. MCPPromptWrapper API

**继承**：`Tool`

| 属性 | 值 |
|------|-----|
| `name` | `mcp_{server}_prompt_{name}` |
| `_plugin_discoverable` | `False` |
| `read_only` | `True` |
| `parameters` | 动态生成（来自 prompt arguments） |

### 构造

```python
MCPPromptWrapper(client: MCPClient, prompt_info: dict, server_name: str = "default", timeout: int = 30)
```

### execute(**kwargs)

调用 `client.get_prompt(name, kwargs)`，返回渲染后的消息文本。

## 3. create_mcp_prompts()

```python
async def create_mcp_prompts(client: MCPClient) -> list[MCPPromptWrapper]
```

连接 MCP server（如未连接），列出所有 prompts，创建包装列表。

## 4. JSON-RPC 协议

| method | params | 说明 |
|--------|--------|------|
| `prompts/list` | `{}` | 列出提示词模板 |
| `prompts/get` | `{"name": "...", "arguments": {...}}` | 获取并渲染提示词 |
