# Step 97 Proposal: 内部会话过滤 + 近期历史注入

## 1. 问题背景

当前 `read_unprocessed_history` 返回所有未处理的历史条目，不区分会话。cron、dream、heartbeat 等内部会话的历史不应泄漏到普通用户会话的 prompt 中。

参考实现提供了 `read_recent_history_for_prompt(since_cursor, session_key, unified_session)`，按 session_key 过滤历史，并在 unified_session 模式下合并非内部会话的历史。

同时 context.py 的 `build_system_prompt` 尚未注入近期历史（# Recent History 段），`include_memory_recent_history` 目前只注入长期记忆。

## 2. 目标

1. 新增常量 `_INTERNAL_HISTORY_SESSION_PREFIXES = ("cron:", "dream:")` 和 `_INTERNAL_HISTORY_SESSION_KEYS = {"heartbeat"}`
2. 新增 `_is_internal_history_session(session_key)` 类方法
3. 新增 `read_recent_history_for_prompt(since_cursor, session_key, unified_session)` 方法
4. context.py `build_system_prompt` 新增 `session_key` / `unified_session` 参数，注入 `# Recent History` 段

## 3. 非目标

- 不实现 `truncate_text_to_tokens`（step98），近期历史先用字符截断
- 不修改 `read_unprocessed_history`（step96 已完成）

## 4. 验收标准

1. 内部会话（cron:/dream:/heartbeat）被正确识别
2. 指定 session_key 时只返回该会话的历史
3. unified_session=True 时返回该会话 + 所有非内部会话的历史
4. context.py 注入 # Recent History 段
5. 现有测试全部通过
