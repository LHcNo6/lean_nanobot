# Step 94 API 契约

## MemoryStore._write_entries（内部方法）

```python
def _write_entries(self, entries: list[dict[str, Any]]) -> None
```

- **功能**：原子覆盖写入 history.jsonl
- **参数**：entries — 要写入的条目列表
- **副作用**：创建临时文件 → fsync → os.replace → 目录 fsync；异常时清理临时文件
- **异常**：写入失败时抛出原异常，临时文件已清理
