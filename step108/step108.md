# step108：Legacy HISTORY.md 迁移

## 解决的问题

旧版使用 HISTORY.md 存储历史，新版使用 history.jsonl。缺少自动迁移机制。

## 实现

1. 新增 3 个正则常量：`_LEGACY_ENTRY_START_RE`、`_LEGACY_TIMESTAMP_RE`、`_LEGACY_RAW_MESSAGE_RE`
2. 新增 `legacy_history_file` 属性
3. 新增 `migrate_legacy_history()` 公共方法：解析 → 写入 → 设置 cursor → 备份原文件
4. 新增 6 个辅助方法：`_parse_legacy_history`、`_split_legacy_history_chunks`、`_should_start_new_legacy_chunk`、`_is_raw_legacy_chunk`、`_legacy_fallback_timestamp`、`_next_legacy_backup_path`

## 文件修改清单

| 文件 | 操作 |
|------|------|
| `memory.py` | 修改：+re 导入 +3 正则 +legacy_history_file +7 个迁移方法 |
| `tests/test_legacy_migration.py` | 新建（10 测试） |
| 规范文档 + step108.md | 新建 |

## 测试结果

10 passed in 0.37s

## 下一步

**step109**：`_format_messages` 格式对齐 + `raw_archive` 对齐。
