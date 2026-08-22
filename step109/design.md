# Step 109 Design: _format_messages + raw_archive 格式对齐

## 实现思路

### 1. _format_messages 格式对齐

**修改前（旧多行格式）：**
```
[role]
content
[tool_calls: ...]
[tool_result for tool: name]
```

**修改后（参考实现单行格式）：**
```
[timestamp] ROLE [tools: tool1, tool2]: content
```

实现：
```python
@staticmethod
def _format_messages(messages: list[dict]) -> str:
    lines = []
    for message in messages:
        if not message.get("content"):
            continue
        tools = f" [tools: {', '.join(message['tools_used'])}]" if message.get("tools_used") else ""
        lines.append(
            f"[{message.get('timestamp', '?')[:16]}] "
            f"{message['role'].upper()}{tools}: {message['content']}"
        )
    return "\n".join(lines)
```

关键变化：
- 跳过无 content 的消息
- 使用 `timestamp` 字段（截断到 16 字符），缺失用 `?`
- 使用 `tools_used` 字段（而非 `tool_calls`）
- role 大写
- 单行格式，消息间用换行分隔

### 2. raw_archive 对齐

**修改前：**
```python
def raw_archive(self, messages, *, max_chars=None, session_key=None):
    text = self._format_messages(messages)
    if len(text) > limit:
        text = truncate_text(text, limit)
    return self.append_history(f"[RAW] {text}", session_key=session_key)
```

**修改后：**
```python
def raw_archive(self, messages, *, max_chars=None, session_key=None):
    from step109.runtime_context import public_history_messages
    limit = max_chars if max_chars is not None else _RAW_ARCHIVE_MAX_CHARS
    formatted = truncate_text(
        self._format_messages(public_history_messages(messages)),
        limit,
    )
    self.append_history(
        f"[RAW] {len(messages)} messages\n{formatted}",
        session_key=session_key,
    )
    logger.warning("Memory consolidation degraded: raw-archived {} messages", len(messages))
```

关键变化：
- 使用 `public_history_messages` 过滤运行时上下文等内部标记
- 前缀从 `[RAW] {text}` 改为 `[RAW] N messages\n{formatted}`（带消息计数）
- 新增 `logger.warning` 记录降级事件
- 返回值从 cursor 改为 None（与参考实现一致，raw_archive 不返回 cursor）

### 3. 兼容性

Consolidator.archive 调用 `MemoryStore._format_messages(messages_to_summarize)`，新格式对 LLM 摘要生成更友好（单行更紧凑），不影响功能。

## 文件修改清单

| 文件 | 操作 |
|------|------|
| `memory.py` | 修改：_format_messages 格式 + raw_archive 对齐 + logging 导入 |
| `tests/test_format_raw_archive.py` | 新建（10 测试） |
| `proposal.md` / `design.md` / `api-spec.md` | 新建 |
