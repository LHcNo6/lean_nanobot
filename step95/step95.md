# step95：append_history 安全增强

## 解决的问题

当前 `append_history` 直接写入原始内容，存在模板泄漏（`<think` 前缀等）和超限无告警两个隐患。

## 实现

1. 导入 `strip_think`，写入前清理内容
2. 新增 `_oversize_logged` flag，超限首次 `logger.warning` 后限流
3. raw 非空但 strip 后为空时持久化空串（不回退 raw，避免 undo strip_think 保证）
4. 空内容时 debug 日志

## 文件修改清单

| 文件 | 操作 |
|------|------|
| `memory.py` | 修改：+strip_think 导入 +_oversize_logged +append_history 增强 |
| `tests/test_memory_append_safety.py` | 新建（13 测试） |
| 规范文档 + step95.md | 新建 |

## 测试结果

13 passed in 0.33s

## 下一步

**step96**：数据校验层（`_valid_cursor` + `_valid_history_payload` + `_iter_valid_entries`）+ cursor 对齐，防止外部写入破坏 cursor 单调性。
