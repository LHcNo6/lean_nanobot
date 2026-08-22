# step109：_format_messages + raw_archive 格式对齐

## 解决的问题

_format_messages 使用旧的多行格式，raw_archive 缺少消息计数和 warning 日志。

## 实现

1. `_format_messages` 改为 `[timestamp] ROLE [tools: ...]: content` 单行格式，跳过无 content 消息
2. `raw_archive` 使用 `public_history_messages` 过滤，格式为 `[RAW] N messages\n{formatted}`，并记录 logger.warning

## 文件修改清单

| 文件 | 操作 |
|------|------|
| `memory.py` | 修改：_format_messages 格式 + raw_archive 对齐 |
| `tests/test_format_raw_archive.py` | 新建（10 测试） |
| 规范文档 + step109.md | 新建 |

## 测试结果

10 passed in 0.30s

## 下一步

**step110**：整体回归测试 + 文档更新。
