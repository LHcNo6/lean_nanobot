# Step 88 Proposal: MCP SSE 传输 + 自动重连

## 1. 问题背景

step82-87 的 MCPClient 只支持 stdio 传输（子进程），无法连接远程 MCP server。
nanobot 支持 stdio/SSE/StreamableHTTP 三种传输，并有自动重连机制。

## 2. 目标

在 `tools/mcp.py` 中新增：
1. MCPTransport ABC：传输层抽象（connect/send_request/disconnect）
2. StdioTransport：现有 stdio 传输的提取
3. SseTransport：SSE 传输（urllib + SSE 解析，无外部依赖）
4. MCPClient 接受 transport 参数
5. 自动重连：连接断开时指数退避重连（最多3次）

## 3. 非目标

- 不实现 StreamableHTTP 传输
- 不实现完整的 SSE 事件类型（只处理 message 事件）
- 不实现断线后的会话状态恢复

## 4. 验收标准

1. MCPTransport ABC 定义 connect/send_request/disconnect
2. StdioTransport 提取现有逻辑
3. SseTransport 实现 SSE 连接和请求发送
4. MCPClient 接受 transport 参数
5. 自动重连在连接断开时触发
6. 单元测试通过
