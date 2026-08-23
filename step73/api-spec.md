# Step 73 API Specification

## 1. _ExecSession

**文件**：`tools/exec_session.py`

| 方法 | 签名 | 说明 |
|------|------|------|
| `__init__` | `(session_id, process, command, cwd, timeout)` | 初始化，启动后台读取任务 |
| `poll` | `async (yield_time_ms, max_output_chars) -> _SessionPoll` | 轮询输出 |
| `write` | `async (chars) -> str \| None` | 写入 stdin，返回错误或 None |
| `kill` | `async () -> None` | 杀死进程 |

### _SessionPoll dataclass

| 字段 | 类型 | 说明 |
|------|------|------|
| `output` | str | 本次轮询的新输出 |
| `done` | bool | 进程是否已退出 |
| `exit_code` | int \| None | 退出码 |
| `elapsed_s` | float | 已运行秒数 |
| `timed_out` | bool | 是否超时 |

## 2. ExecSessionManager

| 方法 | 签名 | 说明 |
|------|------|------|
| `start` | `async (command, cwd, env, timeout, yield_time_ms, max_output_chars) -> (str, _SessionPoll)` | 启动会话 |
| `write` | `async (session_id, chars, close_stdin, terminate, yield_time_ms, max_output_chars) -> _SessionPoll` | 写入 stdin 并轮询 |
| `get` | `(session_id) -> _ExecSession \| None` | 获取会话 |

## 3. WriteStdinTool API

**文件**：`tools/exec_session.py`
**继承**：`Tool`

| 属性 | 值 |
|------|-----|
| `name` | `"write_stdin"` |
| `_scopes` | `{"core", "subagent"}` |

### 参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `session_id` | string | 是 | 目标会话 ID |
| `chars` | string | 否 | 要写入的字符 |
| `close_stdin` | boolean | 否 | 是否关闭 stdin |
| `terminate` | boolean | 否 | 是否终止进程 |
| `yield_time_ms` | integer | 否 | 轮询等待毫秒 |
| `max_output_chars` | integer | 否 | 最大输出字符 |

## 4. ExecTool 新增参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `yield_time_ms` | integer | 否 | 会话模式：等待毫秒后返回 session_id |
| `max_output_chars` | integer | 否 | 会话模式：最大输出字符（默认 10000） |

当 yield_time_ms 不为 None 时，启动会话模式，返回：
```
Session started: {session_id}
{output}
[still running] 或 [exit code: N]
```

## 5. ToolContext 新增字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `exec_session_manager` | ExecSessionManager \| None | 会话管理器实例 |

## 6. 工具发现契约

`ToolLoader` 扫描 `tools/exec_session.py` 时发现 `WriteStdinTool`。
最终注册：`write_stdin`（ExecTool 已在 shell.py 中注册为 `exec`）
