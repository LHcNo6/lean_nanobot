# step85：工具注册整合 + 发现验证

## 实现

验证 ToolLoader 能正确发现所有工具类，包括新增的 CliAppsTool 和 ListExecSessionsTool。

检查结果：
- _SKIP_MODULES 合理（base/schema/registry/context/loader/config/file_state/sandbox/mcp/__init__）
- CliAppsTool 能被自动发现（cli_apps.py 不在跳过列表中）
- ListExecSessionsTool 能被自动发现（exec_session.py 不在跳过列表中）
- WriteStdinTool 能被自动发现
- 所有核心工具类都能被发现

新增工具发现集成测试：
- 验证 discover() 返回所有预期工具类
- 验证新增工具在发现列表中
- 验证注册流程（discover → enabled → create → register）
- 验证 cli_apps 启用/禁用控制
- 验证 list_exec_sessions 需要 exec_session_manager

## 文件修改清单

| 文件 | 操作 |
|------|------|
| `tests/test_tool_discovery.py` | 新建（18测试） |
| `proposal.md`/`design.md`/`api-spec.md` | 新建 |

## 测试结果

18 passed
