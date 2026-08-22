# step97：内部会话过滤 + 近期历史注入

## 解决的问题

当前历史不区分会话，cron/dream/heartbeat 等内部会话历史可能泄漏到普通用户 prompt。context.py 也未注入近期历史。

## 实现

1. 新增类常量 `_INTERNAL_HISTORY_SESSION_PREFIXES` / `_INTERNAL_HISTORY_SESSION_KEYS`
2. 新增 `_is_internal_history_session()` 类方法
3. 新增 `read_recent_history_for_prompt()`：按 session_key 过滤，unified_session 模式合并非内部会话
4. context.py `build_system_prompt` 新增 `session_key` / `unified_session` 参数，注入 `# Recent History` 段

## 文件修改清单

| 文件 | 操作 |
|------|------|
| `memory.py` | 修改：+2 常量 +2 方法 |
| `context.py` | 修改：+truncate_text 导入 +2 参数 +近期历史注入 |
| `tests/test_memory_session_filter.py` | 新建（14 测试） |
| 规范文档 + step97.md | 新建 |

## 测试结果

14 passed in 0.40s

## 下一步

**step98**：helpers 新增 `truncate_text_to_tokens` + Consolidator `_truncate_to_token_budget` 对齐（从 chars*4 粗估改为精确 token 截断）。
