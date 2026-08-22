# Step 100 API 契约

## consolidation.py 变更

### Consolidator.__init__
新增参数 `unified_session: bool = False`。

### Consolidator._locks
类型从 `dict[str, asyncio.Lock]` 改为 `weakref.WeakValueDictionary[str, asyncio.Lock]`。

### Consolidator.estimate_session_prompt_tokens（新增）
```python
def estimate_session_prompt_tokens(self, session, *, runtime) -> tuple[int, str]
```
返回 (token_count, source)，source 为 "chain" 或 "fallback"。

### maybe_consolidate_by_tokens（行为变更）
改用 `estimate_session_prompt_tokens` 替代 `sum(estimate_message_tokens)`。
