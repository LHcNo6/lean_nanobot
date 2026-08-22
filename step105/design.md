# Step 100 Design: token 估算对齐 + unified_session + WeakValueDictionary

## 实现

1. 导入 `weakref`
2. `__init__` 新增 `unified_session: bool = False`，存储为 `self.unified_session`
3. `_locks` 改为 `weakref.WeakValueDictionary()`，`get_lock` 改用 `setdefault`
4. 新增 `estimate_session_prompt_tokens`：
   - 获取未归档历史
   - 解析 session.key 获取 channel/chat_id
   - 获取 _last_summary
   - 调用 `_build_messages` 构建 probe（try-except 适配不同签名）
   - 调用 `estimate_prompt_tokens_chain` 返回 (tokens, source)
5. `maybe_consolidate_by_tokens` 改用新方法，estimated <= 0 时回退

## 文件修改清单

| 文件 | 操作 |
|------|------|
| `consolidation.py` | 修改：+weakref +unified_session +WeakValueDictionary +estimate_session_prompt_tokens +maybe_consolidate_by_tokens 改用 |
| `tests/test_consolidator_tokens.py` | 新建 |
| 规范文档 + step100.md | 新建 |
