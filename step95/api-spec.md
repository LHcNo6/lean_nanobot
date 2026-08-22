# Step 95 API 契约

## MemoryStore.append_history（行为变更）

```python
def append_history(
    self,
    entry: str,
    *,
    max_chars: int | None = None,
    session_key: str | None = None,
) -> int
```

- **变更点**：
  1. 写入前调用 `strip_think(raw)` 清理模板泄漏
  2. 超限首次 `logger.warning`，后续限流
  3. raw 非空但 strip 后为空时持久化空串（不回退 raw）
- **返回**：cursor 值（不变）
- **副作用**：可能输出 warning/debug 日志
