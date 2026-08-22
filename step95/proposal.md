# Step 95 Proposal: append_history 安全增强

## 1. 问题背景

当前 `append_history` 直接将原始内容写入 history.jsonl，存在两个安全隐患：
1. **模板泄漏**：内容可能包含未闭合的 `<think` 前缀、`<channel|>` 标记等内部模板片段，写入历史后会在后续 replay/consolidation 时重新注入 prompt。
2. **超限无告警**：超过 `_HISTORY_ENTRY_HARD_CAP` 时静默截断，无法发现调用方忘记设置自己的 cap。

参考实现在 `append_history` 中集成 `strip_think` 清理内容，并对超限条目做首次 `logger.warning` 后限流。

## 2. 目标

1. 导入 `strip_think`，append 前清理内容
2. 超限条目首次 `logger.warning`，后续限流（`_oversize_logged` flag）
3. raw 非空但 strip 后为空时，持久化空串（不回退到 raw，避免 undo strip_think 的保证）
4. 空内容时 debug 日志

## 3. 非目标

- 不修改 `raw_archive`（step109 统一格式）
- 不引入 entry 校验层（step96）
- 不修改 cursor 分配逻辑

## 4. 验收标准

1. 含 `<think>...</think>` 的内容被清理后写入
2. 超限内容被截断且只警告一次
3. raw 非空但 strip 后为空时，记录 content 为空串
4. 现有测试全部通过
