# Step 73 Proposal: ExecSession 交互式执行会话

## 1. 问题背景

step69-70 的 ExecTool 是一次性执行：命令启动后等待完成，超时则杀死。
但长运行命令（如 dev server、交互式 REPL、构建监控）需要：
- 后台运行，不阻塞 agent
- 轮询输出
- 向 stdin 写入输入
- 终止进程

nanobot 通过 `exec_session.py` 实现这些能力：ExecSessionManager 管理会话，
WriteStdinTool 提供 stdin 写入，ExecTool 的 yield_time_ms 参数启动会话模式。

## 2. 目标

1. 新建 `tools/exec_session.py`：
   - `_ExecSession`：管理单个长运行进程（后台读 stdout/stderr，poll，write，kill）
   - `ExecSessionManager`：管理多个会话（start/write/poll）
   - `WriteStdinTool`：向会话写入 stdin 的工具
2. 修改 `tools/shell.py`：ExecTool 添加 `yield_time_ms` 参数，支持会话模式
3. 修改 `context.py`：ToolContext 添加 `exec_session_manager` 字段
4. 修改 `loop.py`：创建 ExecSessionManager 实例

## 3. 非目标

- 不实现 shell_program/login 参数（用默认 shell）
- 不实现 owner_session_key 隔离
- 不实现 idle_timeout 自动清理
- 不实现会话列表/信息查询工具
- 不实现复杂的输出格式化

## 4. 验收标准

1. ExecTool 带 yield_time_ms 时返回 session_id + 初始输出
2. WriteStdinTool 可向会话写入 stdin 并获取新输出
3. 会话完成后自动从管理器移除
4. 不存在的 session_id 返回错误
5. 一次性 exec（无 yield_time_ms）行为不变（向后兼容）
6. 单元测试通过
