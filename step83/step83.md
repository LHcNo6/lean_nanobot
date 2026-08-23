# step83：CliAppsTool（CLI应用白名单工具）

## 实现

新建 `tools/cli_apps.py`：
- CliApp 数据类：name/command/description
- CliAppManager：应用注册/查询/列出/执行（argv 子进程）
- CliAppsTool：工具类，执行已注册的 CLI 应用
- 使用 create_subprocess_exec（argv）而非 shell，更安全
- 未知应用名拒绝执行

修改 `context.py`：
- ToolContext 新增 cli_app_manager 字段

## 文件修改清单

| 文件 | 操作 |
|------|------|
| `tools/cli_apps.py` | 新建 |
| `context.py` | 修改：+cli_app_manager字段 |
| `tests/test_cli_apps.py` | 新建（23测试） |
| `proposal.md`/`design.md`/`api-spec.md` | 新建 |

## 测试结果

23 passed
