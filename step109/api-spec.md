# Step 109 API 契约

## memory.py — MemoryStore 变更

### _format_messages（行为变更，静态方法）
```python
@staticmethod
def _format_messages(messages: list[dict]) -> str
```

**输出格式从多行改为单行：**
```
[timestamp] ROLE [tools: tool1, tool2]: content
```

**规则：**
- 无 `content` 的消息被跳过
- `timestamp` 截断到 16 字符，缺失用 `?`
- 有 `tools_used` 时追加 ` [tools: tool1, tool2]`
- `role` 大写
- 多条消息用换行分隔

### raw_archive（行为变更）
```python
def raw_archive(self, messages: list[dict], *, max_chars: int | None = None, session_key: str | None = None) -> None
```

**变更点：**
1. 内部使用 `public_history_messages(messages)` 过滤内部标记
2. 写入格式为 `[RAW] N messages\n{formatted}`（带消息计数）
3. 调用时记录 `logger.warning("Memory consolidation degraded: raw-archived {} messages", len(messages))`
4. 返回值从 `int`（cursor）改为 `None`

**参数不变：**
- `messages`：要归档的消息列表
- `max_chars`：最大字符数，默认 `_RAW_ARCHIVE_MAX_CHARS`
- `session_key`：会话键
