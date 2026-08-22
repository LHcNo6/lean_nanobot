# Step 97 API 契约

## MemoryStore 新增

### 常量
- `_INTERNAL_HISTORY_SESSION_PREFIXES = ("cron:", "dream:")`
- `_INTERNAL_HISTORY_SESSION_KEYS = {"heartbeat"}`

### _is_internal_history_session（类方法）
```python
@classmethod
def _is_internal_history_session(cls, session_key: str | None) -> bool
```

### read_recent_history_for_prompt
```python
def read_recent_history_for_prompt(
    self,
    since_cursor: int,
    *,
    session_key: str | None,
    unified_session: bool = False,
) -> list[dict[str, Any]]
```

## ContextBuilder.build_system_prompt（参数扩展）
新增 `session_key: str | None = None` 和 `unified_session: bool = False` 参数。
当 `include_memory_recent_history=True` 时注入 `# Recent History` 段。
