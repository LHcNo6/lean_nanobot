# Step 86 Proposal: MCP Resource 支持

## 1. 问题背景

step82 实现了 MCP 基础框架（stdio+JSON-RPC，tools/list/call），但 MCP 协议还支持 Resource 类型——服务器可以暴露可读取的资源（如文件、数据库查询结果等）。当前 MCPClient 不支持 resources/list 和 resources/read。

## 2. 目标

在 `tools/mcp.py` 中新增 MCP Resource 支持：
1. MCPClient 新增 `list_resources()` 方法（resources/list）
2. MCPClient 新增 `read_resource(uri)` 方法（resources/read）
3. MCPResourceWrapper 类：将 MCP resource 包装为只读 native Tool
4. `create_mcp_resources()` 辅助函数：连接 server 并创建所有 resource 包装

## 3. 非目标

- 不实现 resource 模板（resource templates）
- 不实现 resource 订阅（resources/subscribe）
- 不实现 SSE 传输（step88）

## 4. 验收标准

1. MCPClient.list_resources() 发送 resources/list 请求
2. MCPClient.read_resource(uri) 发送 resources/read 请求
3. MCPResourceWrapper 包装 resource 为只读 Tool
4. 工具名格式：mcp_{server}_resource_{name}
5. 单元测试通过
