# step121 需求定义：子代理 announce 模板化与 origin_message_id 透传

## 1. 问题背景

step120 已完成子代理运行配置传播（G1–G4）。step119 末差距分析（路线图 §6）指出，子代理
announce 仍有两类落后 nanobot（G6 / G8）：

- **G6（announce 模板化）**：learn_nano 的 `_announce` 用内联 f-string 拼接文案，而 nanobot
  渲染 `agent/subagent_announce.md` 模板（头部 + Task + Result + Summarize 指令），结构稳定、
  便于渠道复用。
- **G8（origin_message_id 透传）**：learn_nano 的 announce 元数据仅含 `injected_event` /
  `subagent_task_id`，未透传 `origin_message_id`；nanobot 在 announce 元数据中携带
  `origin_message_id`，供结果路由回原消息。

经用户确认，step121 **仅做 G6 模板化 + G8 origin_message_id 透传**；`subagent_channel_display`
通道清洗（G6 通道部分）**推迟到独立 step**（因 nanobot 在展示边界清洗、保留 LLM 全文回放，
learn_nano 无独立展示管线，直接放 `_announce` 会截断 LLM 注入内容）。

## 2. 目标

对齐 nanobot 子代理 announce 的渲染方式与 `origin_message_id` 透传：
- 用 `subagent_announce.md` 模板渲染 announce 内容（替换内联 f-string）；
- 把父消息 id 经 spawn → SubagentManager → `_announce` 透传到 announce 元数据。

## 3. 需求定义（最小增量）

- **F1 — 模板化 announce**：`_announce` 通过模板渲染生成 content；输出须含头部
  `[Subagent '<label>' <status_text>]`、`Task: <task>`、`Result:\n<result>`、Summarize 指令；
  `status_text` 对齐 nanobot（`"completed successfully"` / `"failed"`）。
- **F2 — origin_message_id 透传**：spawn 工具在 `origin` 中携带 `origin_message_id`；
  `_announce` 在 `origin_message_id` 非空时写入 `metadata["origin_message_id"]`。

## 4. 范围与约束

- 不改动 `main.py` 接线；`origin_message_id` 来自 `current_request_context().message_id`
  （同既有 `message_id` 透传路径）。
- 不引入外部模板引擎依赖：用零依赖的 `{{ var }}` 占位替换器（模块级缓存读取模板文件，
  缺失时回退内置常量）。
- 不实现通道清洗（推迟）：保留 LLM 注入的全文结果，与 nanobot 语义一致。
