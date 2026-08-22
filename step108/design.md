# Step 108 Design: Legacy HISTORY.md 迁移

## 实现思路

### 1. 迁移触发

在 `MemoryStore.__init__` 末尾调用 `self._maybe_migrate_legacy_history()`。

触发条件（全部满足）：
- `legacy_history_file`（memory/HISTORY.md）存在
- `history_file`（memory/history.jsonl）不存在或大小为 0

### 2. 解析流程

```
HISTORY.md 文本
  → normalize（换行符统一、strip）
  → _split_legacy_history_chunks（按时间戳行 + 空行分隔拆分）
  → 每个 chunk：
      → _LEGACY_TIMESTAMP_RE 提取时间戳
      → 剩余部分作为 content
      → 分配 cursor（从 1 递增）
  → _write_entries 写入 history.jsonl
  → 设置 cursor 文件 = 最后 cursor
  → 设置 dream_cursor 文件 = 最后 cursor（标记为已处理，不重放）
  → HISTORY.md 重命名为 .bak
```

### 3. 关键正则

```python
_LEGACY_ENTRY_START_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2}[^\]]*)\]\s*")
_LEGACY_TIMESTAMP_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\]\s*")
_LEGACY_RAW_MESSAGE_RE = re.compile(
    r"^\[\d{4}-\d{2}-\d{2}[^\]]*\]\s+[A-Z][A-Z0-9_]*(?:\s+\[tools:\s*[^\]]+\])?:"
)
```

### 4. [RAW] 块特殊处理

`_is_raw_legacy_chunk` 判断当前块是否以 `[RAW]` 开头。RAW 块内的后续消息行（如 `[timestamp] ROLE: content`）不应触发新块拆分，通过 `_should_start_new_legacy_chunk` 中的特殊判断实现。

### 5. Fallback 时间戳

无时间戳的条目使用 `legacy_history_file.stat().st_mtime` 格式化为 `%Y-%m-%d %H:%M`。

### 6. 备份文件命名

- 首选 `HISTORY.md.bak`
- 已存在则 `HISTORY.md.bak.2`、`.bak.3`...

### 7. 容错

迁移过程中任何异常（解析失败、写入失败等）均 `logger.exception` 记录，不抛出，不影响 MemoryStore 正常使用。

## 文件修改清单

| 文件 | 操作 |
|------|------|
| `memory.py` | 修改：+re 导入 +3 正则 +legacy_history_file +7 个迁移方法 |
| `tests/test_legacy_migration.py` | 新建（10 测试） |
| `proposal.md` / `design.md` / `api-spec.md` | 新建 |
