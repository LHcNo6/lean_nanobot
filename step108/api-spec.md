# Step 108 API 契约

## memory.py — MemoryStore 变更

### 新增类常量
```python
_LEGACY_ENTRY_START_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2}[^\]]*)\]\s*")
_LEGACY_TIMESTAMP_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\]\s*")
_LEGACY_RAW_MESSAGE_RE = re.compile(
    r"^\[\d{4}-\d{2}-\d{2}[^\]]*\]\s+[A-Z][A-Z0-9_]*(?:\s+\[tools:\s*[^\]]+\])?:"
)
```

### 新增实例字段
- `legacy_history_file: Path` = `memory_dir / "HISTORY.md"`

### 新增公共方法

```python
def migrate_legacy_history(self) -> int
```
执行迁移，返回迁移的条目数。无 HISTORY.md 或 history.jsonl 已非空时返回 0。

### 新增私有方法

| 方法 | 说明 |
|------|------|
| `_maybe_migrate_legacy_history()` | __init__ 时自动调用，触发迁移 |
| `_parse_legacy_history(text) -> list[dict]` | 解析 HISTORY.md 文本为条目列表 |
| `_split_legacy_history_chunks(text) -> list[str]` | 按时间戳+空行拆分块 |
| `_should_start_new_legacy_chunk(line, current) -> bool` | 判断当前行是否应开始新块 |
| `_is_raw_legacy_chunk(lines) -> bool` | 判断块是否为 [RAW] 类型 |
| `_legacy_fallback_timestamp() -> str` | 无时间戳时用文件 mtime |
| `_next_legacy_backup_path() -> Path` | 生成不冲突的备份文件名 |

### 迁移后状态

- history.jsonl 包含迁移的所有条目，cursor 从 1 递增
- `.cursor` 文件 = 最后一条 cursor
- `.dream_cursor` 文件 = 最后一条 cursor（标记已处理）
- HISTORY.md → HISTORY.md.bak（或 .bak.N）
