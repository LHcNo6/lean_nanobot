# Step 94 Design: _write_entries 原子写

## 原理

原子写的核心是 `os.replace`：在同一文件系统上，rename 操作是原子的。因此先写临时文件并 fsync 确保数据落盘，再 rename，就能保证目标文件不会处于半写状态。

目录 fsync 确保 rename 的元数据也落盘（防止崩溃后目录项未更新）。Windows 上 `os.open(dir, O_RDONLY)` 会抛 PermissionError，用 `suppress(PermissionError)` 跳过——NTFS 本身会同步记录元数据日志。

## 实现

```python
def _write_entries(self, entries):
    tmp_path = self.history_file.with_suffix(self.history_file.suffix + ".tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            for entry in entries:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, self.history_file)
        with suppress(PermissionError):
            fd = os.open(str(self.history_file.parent), os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
```

## 文件修改清单

| 文件 | 操作 |
|------|------|
| `memory.py` | 修改：_write_entries 原子写 |
| `tests/test_memory_atomic_write.py` | 新建 |
| `proposal.md`/`design.md`/`api-spec.md`/`step94.md` | 新建 |
