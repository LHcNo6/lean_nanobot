# step88：MCP SSE 传输 + 自动重连

## 实现

修改 `tools/mcp.py`：
- MCPTransport ABC：传输层抽象（connect/send_request/disconnect/is_connected）
- StdioTransport：从 MCPClient 提取的 stdio 子进程传输
- SseTransport：SSE HTTP 传输（urllib，支持纯JSON和SSE格式响应）
- MCPClient 重构：接受 transport 参数，默认 StdioTransport
- 自动重连：连接断开时指数退避（1s/2s/4s），最多 max_retries 次
- 向后兼容：MCPClient(command, args) 接口不变

## 文件修改清单

| 文件 | 操作 |
|------|------|
| `tools/mcp.py` | 修改：+MCPTransport +StdioTransport +SseTransport +MCPClient重构 +自动重连 |
| `tests/test_mcp.py` | 修改：适配新传输层mock |
| `tests/test_mcp_transport.py` | 新建（23测试） |
| `proposal.md`/`design.md`/`api-spec.md` | 新建 |

## 测试结果

82 passed（23新 + 59旧）
