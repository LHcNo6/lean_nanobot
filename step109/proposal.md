# Step 109 Proposal: _format_messages + raw_archive 格式对齐

## 1. 问题背景

`MemoryStore._format_messages` 当前使用旧的多行格式（`[role]\ncontent\n[tool_calls: ...]`），与参考实现的单行格式（`[timestamp] ROLE [tools: ...]: content`）不一致。`raw_archive` 缺少消息计数前缀和降级 warning 日志，且未使用 `public_history_messages` 过滤内部标记。

## 2. 目标

1. `_format_messages` 改为参考实现格式：`[timestamp] ROLE [tools: ...]: content`，跳过无 content 消息
2. `raw_archive` 使用 `public_history_messages` 过滤内部消息，格式为 `[RAW] N messages\n{formatted}`，并记录 `logger.warning`
3. 保持与 Consolidator 的兼容性（Consolidator 调用 `MemoryStore._format_messages`）

## 3. 非目标

- 不修改 Consolidator 的归档逻辑
- 不修改 append_history 的格式
- 不修改 history.jsonl 的存储结构

## 4. 验收标准

1. `_format_messages` 输出格式为 `[timestamp] ROLE: content`
2. 有 tools_used 时输出 `[tools: tool1, tool2]` 后缀
3. 无 content 的消息被跳过
4. 缺失 timestamp 时使用 `?`
5. `raw_archive` 输出包含 `[RAW] N messages` 前缀
6. `raw_archive` 使用 `public_history_messages` 过滤
7. `raw_archive` 调用时记录 warning 日志
8. `raw_archive` 空消息列表不报错
9. 单元测试通过
