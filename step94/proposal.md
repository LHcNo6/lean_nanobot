# Step 94 Proposal: _write_entries 原子写

## 1. 问题背景

当前 `_write_entries` 直接以 `"w"` 模式打开 history.jsonl 覆盖写入。如果进程在写入过程中崩溃（断电、OOM、kill -9），文件可能处于半写状态，导致 JSONL 解析失败或数据丢失。

参考实现 nanobot 使用原子写模式：先写入临时文件，fsync 刷盘，再 `os.replace` 原子重命名，最后 fsync 目录。这保证了 history.jsonl 要么是完整的旧版本，要么是完整的新版本，不会出现半写状态。

## 2. 目标

将 `MemoryStore._write_entries` 改为原子写：
1. 写入 `.tmp` 临时文件
2. `f.flush()` + `os.fsync(f.fileno())` 刷盘
3. `os.replace(tmp, history_file)` 原子重命名
4. fsync 父目录（Windows 上跳过，NTFS 元数据日志同步）
5. 异常时清理临时文件

## 3. 非目标

- 不修改 `append_history`（追加写本身是原子的，单条 write 不会半写）
- 不修改持久化文件（MEMORY.md/SOUL.md/USER.md）的写入方式
- 不引入文件锁或数据库

## 4. 验收标准

1. `compact_history` 后 history.jsonl 内容正确
2. 写入后无 `.tmp` 残留文件
3. 模拟写入异常时临时文件被清理
4. Windows 上目录 fsync 的 PermissionError 被静默跳过
5. 现有测试全部通过
