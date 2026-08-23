# step87：MCP Prompt 支持

## 实现

修改 `tools/mcp.py`：
- MCPClient 新增 list_prompts()（prompts/list）和 get_prompt(name, arguments)（prompts/get）
- MCPPromptWrapper：将 MCP prompt 模板包装为 native Tool
- 工具名格式：mcp_{server}_prompt_{name}
- 动态参数 schema（从 prompt 的 arguments 构建）
- 支持 content 为 list/dict/str 三种格式
- create_mcp_prompts() 辅助函数

## 文件修改清单

| 文件 | 操作 |
|------|------|
| `tools/mcp.py` | 修改：+2个client方法 +MCPPromptWrapper +create_mcp_prompts |
| `tests/test_mcp_prompt.py` | 新建（19测试） |
| `proposal.md`/`design.md`/`api-spec.md` | 新建 |

## 测试结果

19 passed
