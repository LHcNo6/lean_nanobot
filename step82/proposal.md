# Step 82 Proposal: MCP 协议基础框架

## 1. 问题背景

MCP（Model Context Protocol）允许 AI 模型与外部工具和数据源交互。
nanobot 有完整的 MCP 客户端实现，支持 stdio/SSE 传输、工具发现和调用。

## 2. 目标

新建 `tools/mcp.py`，实现 MCP 基础框架：
1. MCPClient：管理 MCP server 连接（stdio 传输）
2. JSON-RPC 协议：initialize、tools/list、tools/call
3. MCPTool：将 MCP 工具包装为 native Tool
4. 简化版，不依赖外部 mcp SDK，用 asyncio subprocess

## 3. 非目标

- 不实现 SSE/HTTP 传输
- 不实现完整的 MCP 协议（资源、提示词等）
- 不实现自动重连
- 不实现多 server 管理

## 4. 验收标准

1. MCPClient 类存在，支持 connect/disconnect
2. 支持 JSON-RPC request/response
3. MCPTool 包装类存在
4. 单元测试通过（用 mock subprocess）
