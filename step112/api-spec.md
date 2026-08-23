# Step112 接口契约（api-spec）

本文件定义 step112 同步 `cli_apps` / `list_exec_sessions` 后的对外契约，供实现与测试对齐。

## B1：子代理工具集（scope="subagent"）

`SubagentManager._build_tools()` 返回的注册表**恰含且只含**以下 **13** 个工具：

| 工具名 | 来源文件 | 说明 |
| --- | --- | --- |
| `exec` | exec_session.py (WriteStdinTool 之外的基础 shell 工具) | 命令执行 |
| `read_file` | filesystem / read_file | 读文件 |
| `write_file` | filesystem | 写文件 |
| `edit_file` | filesystem | 编辑文件 |
| `list_dir` | filesystem | 列目录 |
| `find_files` | filesystem | 查找文件 |
| `grep` | search | 内容搜索 |
| `web_search` | web | 网络搜索 |
| `web_fetch` | web | 网页抓取 |
| `write_stdin` | exec_session.py (WriteStdinTool) | 长运行会话写 stdin |
| `apply_patch` | apply_patch | 应用补丁 |
| `run_cli_app` | **cli_apps.py (CliAppsTool)** ★新增 | 执行已注册 CLI 应用 |
| `list_exec_sessions` | **exec_session.py (ListExecSessionsTool)** ★新增 | 列出活跃执行会话 |

★ 为本 step 新增同步的工具。

```python
SUBAGENT_TOOL_NAMES = {
    "exec", "read_file", "write_file", "edit_file", "list_dir",
    "find_files", "grep", "web_search", "web_fetch", "write_stdin",
    "apply_patch", "run_cli_app", "list_exec_sessions",
}
assert set(registry._tools.keys()) == SUBAGENT_TOOL_NAMES
```

## B2：核心专属工具不可见于子代理

以下工具 scope 不含 `"subagent"`，在子代理注册表中**必须缺席**：

```
spawn, message, create_goal, update_goal, echo, generate_image, glob
```

（对应测试：`test_build_tools_excludes_core_only_tools`）

## B3：CliAppsTool 契约

- 类：`step112.tools.cli_apps.CliAppsTool`
- `name` 属性：`"run_cli_app"`
- `_scopes`：`{"core", "subagent"}`
- `read_only`：`False`
- `enabled(ctx)`：读 `getattr(getattr(ctx, "config", None), "cli_apps", None)` 的
  `.enable`，缺省 `True`。
- `create(ctx)`：取 `getattr(ctx, "cli_app_manager", None)`，为 `None` 时新建空
  `CliAppManager()`，返回 `CliAppsTool(manager=..., workspace=ctx.workspace)`。
- `execute(name, args=None, working_dir=None, timeout=None)`：
  - `name` 为空 → `ToolResult.error("Error: 'name' is required.")`
  - 未知应用 → `ToolResult.error("Error: Unknown CLI app '...'")`
  - 超时 → `ToolResult.error("Error: CLI app '...' timed out after ... seconds")`
  - 成功 → 返回 stdout+stderr 文本

## B4：ListExecSessionsTool 契约

- 类：`step112.tools.exec_session.ListExecSessionsTool`
- `name` 属性：`"list_exec_sessions"`
- `_scopes`：`{"core", "subagent"}`
- `read_only`：`True`
- `enabled(ctx)`：要求 `getattr(ctx, "exec_session_manager", None)` 非 `None`。
- `execute()`：
  - `exec_session_manager` 为 `None` → `ToolResult.error("Error: No exec session manager available.")`
  - `manager.list()` 抛异常 → `ToolResult.error("Error listing exec sessions: ...")`
  - 无会话 → 返回字符串 `"No active exec sessions."`
  - 有会话 → 每行一条：
    ```
    <session_id> | <status> | elapsed=<elapsed_s>.1f s | cwd=<cwd> | <command 截断120>
    ```

## B5：ExecSessionManager.list() 契约

- 签名：`def list(self) -> list[ExecSessionInfo]`
- 返回：当前所有会话的 `ExecSessionInfo` 列表，按 `session_id` 排序。
- `ExecSessionInfo` 字段：`session_id: str`、`command: str`、`cwd: str`、
  `elapsed_s: float`、`status: str`（`"running"`/`"exited"`）、
  `returncode: int | None`。
- `status` 判定：`process.returncode is not None` → `"exited"`，否则 `"running"`。
- `elapsed_s`：`time.monotonic() - session.started_at`。

## B6：ToolContext 契约补充

- 新增字段（context.py）：`cli_app_manager: Any = None`
- 与既有 `exec_session_manager` 等字段并列，默认 `None`，向后兼容。

## 测试映射

| 契约 | 测试 |
| --- | --- |
| B1 / B2 | tests/test_subagent_tool_isolation.py::TestBuildToolsWhitelist |
| B3 | tests/test_cli_apps.py（端口自 step85） |
| B4 / B5 | tests/test_list_exec_sessions.py（端口自 step85） |
| B6 | context.py 字段存在即满足（端口单测隐式覆盖） |

> 全部测试使用 mock provider / 构造数据，禁止真实网络与 API 调用。
