# step91：MyTool 完整 AgentLoop 引用 + 嵌套属性访问

## 实现

修改 `tools/self.py`：
- 新增 `_resolve_nested_path(obj, parts)` 函数：逐段解析嵌套属性，每段检查安全边界
- `_get_runtime_value` 支持点分嵌套路径（如 `config.exec.timeout`）
- 新增 `agent` 顶级 key，映射到 `ctx.agent_loop`
- `_do_get` 捕获 PermissionError 返回 ToolResult.error
- `_has_key` 支持嵌套路径检测（第一段是已知顶级 key 即可）
- set 操作仍然只支持单层白名单属性

修改 `context.py`：
- ToolContext 新增 `agent_loop` 字段

## 文件修改清单

| 文件 | 操作 |
|------|------|
| `tools/self.py` | 修改：+嵌套属性解析 +agent key +安全边界检查 |
| `context.py` | 修改：+agent_loop字段 |
| `tests/test_my_tool_nested.py` | 新建（23测试） |
| `proposal.md`/`design.md`/`api-spec.md` | 新建 |

## 测试结果

41 passed（23新 + 18旧）
