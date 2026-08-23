# step125 api-spec：通道清洗（G6 通道部分）

## 1. `step125.utils.subagent_channel_display`

### 1.1 `scrub_subagent_announce_body(content: str) -> str`

把完整的子代理 announce 文本清洗为人类友好的通道文本。

- **入参**：
  - `content: str` — 完整 announce 文本（含 `[Subagent ...]` 头、`Task:`、`Result:`、
    `Summarize this naturally ...` 尾标记）。
- **返回**：`str` — 仅含 header + 截断 `Result:` 正文的清洗文本（≤约 800 字符 + header）；
  缺失 `Result:` 段时回退返回 header 或原文。
- **副作用**：无；不修改 `content`，返回新字符串。
- **对齐**：nanobot `utils/subagent_channel_display.scrub_subagent_announce_body`。

### 1.2 `scrub_subagent_messages_for_channel(messages: list[dict[str, Any]]) -> None`

原地清洗消息列表中携带 `subagent_result` 注入标记的消息正文。

- **入参**：
  - `messages: list[dict]` — 消息字典列表（同 `session.messages` 形态）。
- **副作用**：对 `m.get("injected_event") == "subagent_result"` 且 `content` 为字符串的消息，
  原地改写 `m["content"] = scrub_subagent_announce_body(m["content"])`。
- **对齐**：nanobot `utils/subagent_channel_display.scrub_subagent_messages_for_channel`。

## 2. `step125.command.builtin._cmd_history` 行为变更

- **触发**：会话历史中存在 `injected_event == "subagent_result"` 的持久化消息时，该消息在
  `/history` 输出中的正文先经 `scrub_subagent_announce_body` 清洗，再按现有 80 字符截断展示。
- **不变**：`session.messages` 持久化内容不变；隐藏标记消息仍跳过；其余消息渲染逻辑不变。
- **契约**：`/history` 输出的 `subagent_result` 行**不含** `Task:` 与 `Summarize this naturally`
  脚手架文本，仅展示 header 与结果片段。

## 3. announce 模板（`templates/agent/subagent_announce.md`）隐含契约

清洗函数依赖该模板结构：

```
[Subagent '<label>' <status>]      # header（首行以 [Subagent 开头）
Task: <task>                       # 模型脚手架（被清除）
Result:                            # 分隔关键字（小写 result:）
<result>
Summarize this naturally ...       # 尾标记（被清除）
```

若模板结构调整，需同步更新 `_SUBAGENT_CHANNEL_RESULT_MAX_CHARS` 与关键字匹配（当前已对缺失
`Result:` 段做兜底）。
