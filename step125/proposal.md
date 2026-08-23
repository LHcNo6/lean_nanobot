# step125 proposal：通道清洗（G6 通道部分）

## 1. 背景与问题

step121 已把子代理 announce 改造为「模板化」（`subagent_announce.md`），并在 step120–124
逐步补齐了运行配置传播（G1–G5）、origin_message_id（G8）、runtime 逐父同步（G5）、
ToolContext 沙箱 + 多相位状态（G9/G10）、spawn temperature 覆写（G7）。

子代理 announce 正文（`subagent.py._announce` 经 `subagent_announce.md` 渲染）包含三部分：

```
[Subagent '<label>' <status>]

Task: <原始任务>

Result: <子代理实际产出>

Summarize this naturally for the user ...
```

其中 `Task:` 与 `Summarize ...` 是**仅供模型上下文使用**的内部脚手架：模型据此理解任务并
生成面向用户的自然语言摘要，但**人类在通道/历史里不应看到这些脚手架**。nanobot 在
`utils/subagent_channel_display.py` 的展示边界调用 `scrub_subagent_announce_body` 清洗正文，
只保留 `[Subagent ...]` 头 + 截断的 `Result:` 正文（≤800 字符）。

learn_nano 当前把 announce 经 `publish_inbound` 注入父会话，并持久化为 `subagent_result`
assistant 消息；该消息不带 `_hidden_history` 标记，因此 `/history` 命令会原样（截断 80 字符）
展示含 `Task:` / `Summarize` 脚手架的全文——这是 step121 时显式推迟的 **G6 通道部分**。

## 2. 目标与最小范围

- **目标**：在「人类可见的展示边界」清洗子代理 announce 正文，隐藏模型脚手架，仅暴露
  头 + 截断结果；模型上下文 / 持久化历史保留全文（用于 LLM replay）。
- **最小范围**（单 step 单特性）：
  1. 新增 `utils/subagent_channel_display.py`，提供 `scrub_subagent_announce_body` 与
     `scrub_subagent_messages_for_channel`（逐字对齐 nanobot）。
  2. 在 `/history` 展示边界（`command/builtin.py._cmd_history`）对 `injected_event ==
     "subagent_result"` 的消息内容应用清洗后再展示。
- **不做**：改动 `_announce` 的持久化全文（LLM 上下文保持完整）；不改 runner / `_run_subagent`。

## 3. 方案选择

- **复用 nanobot 的纯文本清洗函数**：基于 announce 模板的结构（`[Subagent` 头、`\nresult:\n`
  分隔、`summarize this naturally` 尾标记）做无依赖的字符串解析，零风险、易测。
- **清洗边界选 `/history`**：learn_nano 中唯一会原样展示持久化 `subagent_result` 的人类可见
  表面；子代理 announce 本身不直接作为气泡经 `ChannelManager` 出站（仅注入 LLM），故无需在
  outbound 派发层清洗。
- **不改持久化**：清洗只在「渲染时」发生，`session.messages` 中仍存全文，保证 LLM replay 完整。

## 4. 验收

- 单元测试覆盖：`scrub_subagent_announce_body` 的 header 保留、Result 截断、`Task:` 与
  `Summarize` 脚手架移除、无 Result 段兜底；`scrub_subagent_messages_for_channel` 原地改写。
- 集成测试：`/history` 输出中 `subagent_result` 行不含 `Task:` / `Summarize`，且含结果片段。
- 全量测试失败数与 step124 基线持平（25），不引入回归。
