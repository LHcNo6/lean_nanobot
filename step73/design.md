# Step 73 Design: ExecSession

## 1. 架构

```
tools/exec_session.py（新建）
  ├── _ExecSession              单个会话（进程+输出缓冲+stdin）
  ├── ExecSessionManager        会话管理器（start/write/poll）
  └── WriteStdinTool            stdin 写入工具

tools/shell.py（修改）
  └── ExecTool.execute          +yield_time_ms 参数，支持会话模式

context.py（修改）
  └── ToolContext               +exec_session_manager 字段

loop.py（修改）
  └── AgentLoop                 创建 ExecSessionManager 实例
```

## 2. _ExecSession

```python
class _ExecSession:
    def __init__(session_id, process, command, cwd, timeout)
    # 后台任务：_stdout_task, _stderr_task 持续读取流
    # 输出缓冲：_chunks: list[str]
    async def poll(yield_time_ms, max_output_chars) -> _SessionPoll
    async def write(chars) -> str | None  # 返回错误或 None
    async def kill() -> None
```

poll 流程：
1. 等待 yield_time_ms 或进程退出
2. 超时检查（超过 deadline 则 kill）
3. 收集并清空输出缓冲
4. 返回 _SessionPoll(output, done, exit_code, elapsed_s, timed_out)

## 3. ExecSessionManager

```python
class ExecSessionManager:
    def __init__(max_sessions=8)
    async def start(command, cwd, env, timeout, yield_time_ms, max_output_chars) -> (session_id, poll)
    async def write(session_id, chars, close_stdin, terminate, yield_time_ms, max_output_chars) -> poll
    def get(session_id) -> _ExecSession | None
```

start 流程：
1. 检查会话数上限
2. 创建子进程（stdin=PIPE, stdout=PIPE, stderr=PIPE）
3. 创建 _ExecSession，启动后台读取任务
4. 首次 poll（等待 yield_time_ms）
5. 如果已完成，从管理器移除
6. 返回 session_id + poll

## 4. WriteStdinTool

参数：session_id（必填）, chars（可选）, close_stdin（可选）, terminate（可选）, yield_time_ms（可选）, max_output_chars（可选）

流程：
1. 查找会话
2. 写入 chars（如果有）
3. 关闭 stdin（如果 close_stdin）
4. 终止进程（如果 terminate）
5. poll 输出
6. 返回格式化结果

## 5. ExecTool 会话模式

execute 新增参数：yield_time_ms（可选）, max_output_chars（可选）

当 yield_time_ms 不为 None 时：
1. 准备命令（同一次性模式）
2. 调用 ctx.exec_session_manager.start(...)
3. 返回格式化结果：包含 session_id 和输出

格式化：
```
Session started: {session_id}
{output}
[still running] 或 [exit code: N]
```

## 6. 测试策略

- 用 `python -c "import time; time.sleep(0.5); print('done')"` 测试短运行会话
- 用 `python -c "import sys; sys.stdin.read()"` 测试 stdin 写入
- 测试会话完成后自动移除
- 测试不存在的 session_id
- 测试一次性 exec 不变（回归）
