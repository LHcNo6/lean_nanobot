# Step 87 Proposal: MCP Prompt 支持

## 1. 问题背景

step86 实现了 MCP Resource 支持，但 MCP 协议还支持 Prompt 类型——服务器可以暴露可复用的提示词模板，支持参数填充。当前 MCPClient 不支持 prompts/list 和 prompts/get。

## 2. 目标

在 `tools/mcp.py` 中新增 MCP Prompt 支持：
1. MCPClient 新增 `list_prompts()` 方法（prompts/list）
2. MCPClient 新增 `get_prompt(name, arguments)` 方法（prompts/get）
3. MCPPromptWrapper 类：将 MCP prompt 包装为 native Tool
4. `create_mcp_prompts()` 辅助函数

## 3. 非目标

- 不实现 prompt 消息的复杂渲染（只返回文本）
- 不实现 SSE 传输（step88）

## 4. 验收标准

1. MCPClient.list_prompts() 发送 prompts/list
2. MCPClient.get_prompt(name, arguments) 发送 prompts/get
3. MCPPromptWrapper 包装 prompt 为工具
4. 工具名格式：mcp_{server}_prompt_{name}
5. 单元测试通过
