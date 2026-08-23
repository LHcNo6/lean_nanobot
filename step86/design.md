# Step 86 Design: MCP Resource 支持

## 1. 架构

```
tools/mcp.py（修改）
  ├── MCPClient                    已有
  │   ├── +list_resources()        新增：resources/list
  │   └── +read_resource(uri)      新增：resources/read
  ├── MCPTool                      已有
  ├── +MCPResourceWrapper(Tool)    新增：resource 包装为只读 Tool
  └── +create_mcp_resources()      新增：辅助函数
```

## 2. MCP Resource 协议

### resources/list
请求：`{"jsonrpc": "2.0", "id": N, "method": "resources/list", "params": {}}`
响应：`{"result": {"resources": [{"uri": "...", "name": "...", "description": "...", "mimeType": "..."}]}}`

### resources/read
请求：`{"jsonrpc": "2.0", "id": N, "method": "resources/read", "params": {"uri": "..."}}`
响应：`{"result": {"contents": [{"uri": "...", "mimeType": "text/plain", "text": "..."}]}}`

## 3. MCPResourceWrapper

- 继承 Tool
- _plugin_discoverable = False（手动注册）
- 工具名：`mcp_{server}_resource_{sanitized_name}`
- 无参数（resource URI 固定）
- read_only = True
- execute() 调用 client.read_resource(uri)

## 4. 执行流程

1. list_resources：发送 resources/list，解析返回的 resources 数组
2. read_resource：发送 resources/read，提取 contents[].text
3. MCPResourceWrapper.execute：调用 read_resource，返回文本内容

## 5. 测试策略

- list_resources 请求构造
- read_resource 请求构造
- MCPResourceWrapper 工具名格式
- MCPResourceWrapper 无参数 schema
- MCPResourceWrapper.execute 成功/失败
- create_mcp_resources 辅助函数
