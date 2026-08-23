# Step 87 Design: MCP Prompt 支持

## 1. 架构

```
tools/mcp.py（修改）
  ├── MCPClient                    已有
  │   ├── +list_prompts()          新增：prompts/list
  │   └── +get_prompt(name, args)  新增：prompts/get
  ├── MCPTool                      已有
  ├── MCPResourceWrapper           已有（step86）
  ├── +MCPPromptWrapper(Tool)      新增：prompt 包装为工具
  └── +create_mcp_prompts()        新增：辅助函数
```

## 2. MCP Prompt 协议

### prompts/list
请求：`{"jsonrpc": "2.0", "id": N, "method": "prompts/list", "params": {}}`
响应：`{"result": {"prompts": [{"name": "...", "description": "...", "arguments": [{"name": "...", "description": "...", "required": true}]}]}}`

### prompts/get
请求：`{"jsonrpc": "2.0", "id": N, "method": "prompts/get", "params": {"name": "...", "arguments": {"key": "value"}}}`
响应：`{"result": {"description": "...", "messages": [{"role": "user", "content": {"type": "text", "text": "..."}}]}}`

## 3. MCPPromptWrapper

- 继承 Tool
- _plugin_discoverable = False
- 工具名：`mcp_{server}_prompt_{name}`
- 参数：来自 prompt 的 arguments 定义（动态生成 schema）
- execute()：调用 client.get_prompt(name, arguments)，返回渲染后的消息文本

## 4. 动态参数 schema

从 prompt 的 arguments 列表生成 JSON Schema：
```python
{
    "type": "object",
    "properties": {arg["name"]: {"type": "string", "description": arg["description"]}},
    "required": [arg["name"] for arg in arguments if arg.get("required")],
}
```

## 5. 测试策略

- list_prompts 请求构造
- get_prompt 请求构造
- MCPPromptWrapper 工具名格式
- MCPPromptWrapper 动态参数 schema
- MCPPromptWrapper.execute 成功/失败
- create_mcp_prompts 辅助函数
