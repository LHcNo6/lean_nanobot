# step94：_write_entries 原子写

## 解决的问题

当前 `_write_entries` 直接以 `"w"` 模式覆盖写入 history.jsonl。进程在写入过程中崩溃时，文件可能处于半写状态，导致 JSONL 解析失败或数据丢失。

## 原理

原子写核心是 `os.replace`：同一文件系统上 rename 是原子操作。先写临时文件并 fsync 确保数据落盘，再 rename，保证目标文件不会半写。目录 fsync 确保 rename 元数据落盘；Windows 上目录 fsync 抛 PermissionError，NTFS 元数据日志已同步，故跳过。

## 实现

`_write_entries` 改为：tmp 文件写入 → flush + fsync → os.replace → 目录 fsync（suppress PermissionError）；异常时 `tmp_path.unlink(missing_ok=True)` 清理后重抛。

## 文件修改清单

| 文件 | 操作 |
|------|------|
| `memory.py` | 修改：_write_entries 原子写 |
| `tests/test_memory_atomic_write.py` | 新建（6 测试） |
| 规范文档 + step94.md | 新建 |

## 测试结果

35 passed（6 新 + 29 旧）in 0.58s

## 暴露的问题

- `append_history` 未集成 `strip_think`，可能将模板泄漏写入历史（step95）。
- 缺少 entry 校验层，外部写入的畸形数据可能破坏 cursor 单调性（step96）。

## 下一步

**step95**：`append_history` 集成 `strip_think` 防模板泄漏 + oversize 日志限流 + 空内容处理。
