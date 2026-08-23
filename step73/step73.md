# step73：ExecSession 交互式执行会话

## 1. 问题背景

step69-70 的 ExecTool 是一次性执行：命令启动后等待完成，超时则杀死。
但长运行命令（dev server、交互式 REPL、构建监控）需要后台运行、轮询输出、
向 stdin 写入输入、终止进程。

## 2. 实现

新建 `tools/exec_session.py`：
- `_ExecSession`：管理单个长运行进程（后台读 stdout/stderr，输出缓冲，poll，write，kill）
- `ExecSessionManager`：管理多个会话（start/write/poll/get，max_sessions=8）
- `WriteStdinTool`：向会话写入 stdin 的工具（session_id/chars/close_stdin/terminate）

修改：
- `tools/shell.py`：ExecTool 添加 `yield_time_ms`/`max_output_chars` 参数，支持会话模式
- `context.py`：ToolContext 添加 `exec_session_manager` 字段
- `loop.py`：创建 ExecSessionManager 实例并传入 ToolContext

## 3. 会话模式流程

1. ExecTool 带 `yield_time_ms` → 调用 `session_manager.start(...)`
2. 启动子进程（stdin=PIPE/stdout=PIPE/stderr=PIPE），后台任务持续读取流
3. 等待 yield_time_ms 或进程退出，收集输出
4. 返回 `Session started: {id}` + 输出 + `[still running]` 或 `[exit code: N]`
5. WriteStdinTool 用 session_id 写入 stdin/关闭 stdin/终止进程，再次轮询输出

## 4. 文件修改清单

| 文件 | 操作 |
|------|------|
| `tools/exec_session.py` | 新建：_ExecSession + ExecSessionManager + WriteStdinTool |
| `tools/shell.py` | 修改：+yield_time_ms 参数 + _execute_session 方法 |
| `context.py` | 修改：+exec_session_manager 字段 |
| `loop.py` | 修改：创建 ExecSessionManager + 传入 ToolContext |
| `tests/test_exec_session.py` | 新建：16 个测试 |
| `proposal.md`/`design.md`/`api-spec.md` | 新建 |

## 5. 测试结果

第二阶段完整回归：**217 passed**
- test_exec.py: 33（step69 基础）
- test_exec_enhanced.py: 31（step70 增强）
- test_exec_session.py: 16（step73 会话）
- test_web_fetch.py: 24（step71）
- test_web_search.py: 23（step72）
- test_filesystem.py: 19（step65）
- test_edit_file.py: 26（step66）
- test_list_dir.py: 15（step67）
- test_search.py: 30（step68）

## 6. 技术债

- Windows 上 create_subprocess_shell 的 stdin 管道有限制（平台问题）
- 无 owner_session_key 隔离
- 无 idle_timeout 自动清理
- 无会话列表/信息查询工具
- 无 shell_program/login 支持
