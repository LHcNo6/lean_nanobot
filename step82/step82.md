# step82：MCP 协议基础框架

## 实现

新建 `tools/mcp.py`：
- MCPClient：stdio 传输 + JSON-RPC 协议（initialize/tools/list/tools/call）
- MCPTool：将 MCP server 工具包装为 native Tool
- _sanitize_tool_name：工具名清理
- _jsonrpc_request：JSON-RPC 请求构造
- create_mcp_tools：辅助函数，连接 server 并创建所有工具
- MCPTool._plugin_discoverable=False（手动注册，不自动发现）

## 文件修改清单

| 文件 | 操作 |
|------|------|
| `tools/mcp.py` | 新建 |
| `tests/test_mcp.py` | 新建（23测试） |
| `proposal.md`/`design.md`/`api-spec.md` | 新建 |

## 测试结果

23 passed
