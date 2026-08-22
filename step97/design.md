# Step 97 Design: 内部会话过滤 + 近期历史注入

## 原理

### 内部会话识别
- 前缀匹配：session_key 以 "cron:" 或 "dream:" 开头
- 精确匹配：session_key 为 "heartbeat"

### read_recent_history_for_prompt
- session_key=None：返回所有未处理历史
- unified_session=False：只返回 session_key 完全匹配的条目
- unified_session=True：返回 session_key 匹配的条目 + 所有非内部会话的条目（跨会话共享用户历史）

### context.py 注入
在长期记忆段之后、skills 之前，当 include_memory_recent_history=True 时：
1. 调用 read_recent_history_for_prompt
2. 取最近 _MAX_RECENT_HISTORY 条
3. 格式化为 "- [timestamp] content"
4. 字符截断（step98 后改为 token 截断）
5. 追加为 "# Recent History" 段

## 文件修改清单

| 文件 | 操作 |
|------|------|
| `memory.py` | 修改：+2 常量 +2 方法 |
| `context.py` | 修改：build_system_prompt +session_key/unified_session 参数 +近期历史注入 |
| `tests/test_memory_session_filter.py` | 新建 |
| 规范文档 + step97.md | 新建 |
