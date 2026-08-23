# step86：MCP Resource 支持

## 实现

修改 `tools/mcp.py`：
- MCPClient 新增 list_resources()（resources/list）和 read_resource(uri)（resources/read）
- MCPResourceWrapper：将 MCP resource 包装为只读 native Tool
- 工具名格式：mcp_{server}_resource_{name}
- 无参数（resource URI 固定）
- 支持文本和二进制内容
- create_mcp_resources() 辅助函数

## 文件修改清单

| 文件 | 操作 |
|------|------|
| `tools/mcp.py` | 修改：+2个client方法 +MCPResourceWrapper +create_mcp_resources |
| `tests/test_mcp_resource.py` | 新建（17测试） |
| `proposal.md`/`design.md`/`api-spec.md` | 新建 |

## 测试结果

17 passed
