# Step 102 API 契约

## memory.py — MemoryStore 变更

### build_dream_prompt（行为变更）

```python
def build_dream_prompt(self, *, max_entries: int = 20) -> tuple[str, int] | None
```

**变更点：** prompt 前缀从硬编码字符串改为 `self._dream_template()`。

**prompt 结构：**
```
{_dream_template()}

## Current Memory Files
{files_section}

## Conversation History
[timestamp] truncated_content
...
```

**返回值：** `(prompt, last_cursor)` 或 `None`（无未处理历史时）。

**无其他 API 变更。**
