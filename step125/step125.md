# step125 配套文档：通道清洗（G6 通道部分）

## 1. 问题背景

step120–124 已对齐子代理运行配置传播（G1–G5）、announce 模板化 / origin_message_id（G6 模板部分 + G8）、
ToolContext 沙箱 + 多相位状态（G9/G10）、spawn temperature 覆写（G7）。路线图 §6 仅剩一项：
**G6 通道部分——清洗 announce 正文**（step121 时显式推迟）。

子代理 announce（`subagent_announce.md` 渲染）含三部分：头、`Task:`（模型任务上下文）、
`Result:`（实际产出），以及尾部 `Summarize this naturally ...` 模型专用指令。
`Task:` 与 `Summarize` 仅供 LLM 理解任务并生成自然语言摘要，**人类在通道/历史中不应看到**。

learn_nano 把 announce 经 `publish_inbound` 注入父会话并持久化为 `subagent_result` assistant 消息
（不带 `_hidden_history` 标记），因此 `/history` 会原样（截断 80 字符）展示含脚手架的全文。

## 2. 本 step 解决的问题与原因

**解决**：在「人类可见的展示边界」清洗 announce 正文，隐藏模型脚手架，仅暴露头 + 截断结果；
模型上下文 / 持久化历史保留全文（用于 LLM replay）。

**为什么这样做**：nanobot 在 `utils/subagent_channel_display.py` 展示边界调用
`scrub_subagent_announce_body` 做同样清洗；这是子代理子系统对齐 nanobot 的最后一处差距。
实现上零风险（纯文本解析）、易测，且**仅在渲染时发生**，不改动持久化与 runner。

## 3. 原理思路与具体实现

### 3.1 调研关键事实

- nanobot `subagent_channel_display.py` 的清洗逻辑：
  - 归一化换行、取 `[Subagent` 头；
  - 大小写不敏感定位 `\nresult:\n`（回退 `\nresult:`）取其后正文；
  - 删除尾部 `summarize this naturally` 标记及之后内容；
  - `Result:` 正文截断至 `_SUBAGENT_CHANNEL_RESULT_MAX_CHARS = 800`（加 `…`）；
  - 返回 `头 + 双换行 + 正文`；缺失 Result 段时回退 header/原文。
- learn_nano 的 `subagent_announce.md` 结构与 nanobot 一致（头、`Task:`、`Result:`、
  `Summarize this naturally ...`），清洗函数可逐字复用。
- 展示边界：learn_nano 中子代理 announce 不直接作为气泡经 `ChannelManager` 出站（仅注入 LLM），
  唯一原样展示持久化 `subagent_result` 的人类可见表面是 `/history` 命令。

### 3.2 实现（2 文件）

- **F1 — `utils/subagent_channel_display.py`**（新增）：提供
  `scrub_subagent_announce_body(content)` 与 `scrub_subagent_messages_for_channel(messages)`，
  逐字对齐 nanobot；不修改入参，返回新字符串。
- **F2 — `command/builtin.py._cmd_history`**：遍历历史时，对
  `m.get("injected_event") == "subagent_result"` 的消息内容先经
  `scrub_subagent_announce_body` 清洗再按现有 80 字符截断展示；其余消息与持久化内容不变。

### 3.3 不改之处

- `subagent.py._announce` 仍发布全文；`_persist_subagent_followup` 仍持久化全文；
  `get_history`/runner 模型上下文仍用完整 announce（通道清洗只在展示边界发生）。

## 4. 目标与实现

- **目标**：对齐 nanobot G6 通道部分，清洗子代理 announce 的人类可见文本。
- **实现**：清洗工具 + `/history` 边界接线；全量失败数与 step124 基线持平（25）。

## 5. 核心函数/类功能说明

- `scrub_subagent_announce_body`（subagent_channel_display.py）：剥离 Task/Summarize 脚手架，
  返回头 + 截断结果。
- `scrub_subagent_messages_for_channel`：原地清洗消息列表中 subagent_result 正文，供多展示边界复用。
- `_cmd_history`：渲染时对 subagent_result 行应用清洗，隐藏内部脚手架。

## 6. 暴露的问题 / 刻意遗留

- 清洗后的 `Result:` 在 `/history` 仍受 80 字符截断（header 约 40 字符 + 结果片段约 40 字符），
  足够证明脚手架已清除；若未来需要更完整的结果预览，可在该边界放宽截断（独立于本 step）。
- 清洗对 `subagent_announce.md` 模板结构敏感；若模板移除 `Result:` 关键字，函数回退为 header/原文，
  不会抛错或丢失信息（已在测试覆盖兜底路径）。

## 7. 下一 step 待解决

- 至此路线图 §6（step120–124 + 通道清洗）**规划项全部完成**，子代理子系统已高度对齐 nanobot
  （G1–G10 覆盖）。
- 可选增强（非路线图强制项）：放宽 `/history` 对 subagent_result 行的截断长度；
  或将清洗扩展到更多展示边界（会话预览 / WebSocket，若后续引入）。
