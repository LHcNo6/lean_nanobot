# Step 84 Proposal: ListExecSessionsTool（列出执行会话）

## 1. 问题背景

step73 实现了 ExecSessionManager 和 WriteStdinTool，支持长运行命令的交互式执行。
但缺少列出所有活跃会话的工具，agent 无法在上下文切换后恢复 session_id。
nanobot 有 ListExecSessionsTool 用于此目的。

## 2. 目标

在 `tools/exec_session.py` 中：
1. ExecSessionManager 新增 list() 方法，返回会话摘要列表
2. 新增 ExecSessionInfo 数据类（session_id/command/cwd/elapsed_s/status/returncode）
3. 新增 ListExecSessionsTool 工具类，列出活跃执行会话

## 3. 非目标

- 不实现 owner_session_key 隔离
- 不实现 idle_timeout 自动清理
- 不实现会话终止工具（terminate）

## 4. 验收标准

1. ExecSessionManager.list() 返回会话摘要列表
2. ListExecSessionsTool 可以列出会话
3. 无会话时返回提示信息
4. 会话信息包含 session_id/status/elapsed/cwd/command
5. 单元测试通过
