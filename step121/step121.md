# step121：子代理 announce 模板化与 origin_message_id 透传（对齐 nanobot G6 + G8）

## 1. 问题背景

step110–120 已完成子代理核心九维度与运行配置传播（G1–G4）。step119 末差距分析（路线图 §6）
指出 announce 仍落后 nanobot 两项：G6（内联 f-string 未用模板渲染）、G8（未透传
`origin_message_id`）。经用户确认，step121 **仅做 G6 模板化 + G8 透传**，通道清洗
（G6 通道部分）推迟到独立 step。

## 2. 这一 step 解决了什么 / 为什么

把子代理 announce 文案改为模板驱动（结构稳定、便于渠道复用），并把父消息 id 透传到 announce
元数据（对齐 nanobot 的 `origin_message_id` 路由）。均为 announce 表现层与元数据对齐，不改运行
行为，风险低。

方案取舍：模板渲染采用零依赖 `{{ var }}` 替换器（learn_nano 无 jinja2）；通道清洗不做，因 nanobot
在展示边界清洗且保留 LLM 全文，learn_nano 直接放 `_announce` 会截断 LLM 注入结果。

## 3. 原理思路与具体实现

- **模板渲染（G6）**：新增 `templates/agent/subagent_announce.md`（对齐 nanobot 结构）；
  `subagent.py` 增加 `_load_announce_template()`（模块级缓存 + 失败回退内置常量）与
  `_render_subagent_announce(label, status_text, task, result)`（正则替换 `{{ var }}`）；
  `_announce` 由内联 f-string 改为调用渲染器，`status_text` 对齐 nanobot
 （`"completed successfully"` / `"failed"`）。
- **透传（G8）**：`tools/spawn.py` 的 `origin` 新增 `"origin_message_id": req.message_id`；
  `_announce` 读取 `origin.get("origin_message_id")`，非空时写入 `metadata["origin_message_id"]`。

## 4. 目标与实现结果

- announce 内容由 `subagent_announce.md` 模板渲染（F1）；
- `origin_message_id` 从 spawn 经 `_run_subagent` 透传到 announce 元数据（F2）；
- `main.py` / `loop.py` 无改动；仅 `subagent.py` + `tools/spawn.py` + 模板文件。

## 5. 核心函数 / 类功能说明

- `_load_announce_template() -> str`：加载并缓存 announce 模板，失败回退内置常量。
- `_render_subagent_announce(label, status_text, task, result) -> str`：渲染 announce 文本。
- `SubagentManager._announce`：改用模板渲染；新增 `origin_message_id` 元数据透传。
- `SpawnTool.execute`：`origin` 补全 `origin_message_id`。

## 6. 暴露了什么问题

- learn_nano 的 announce「展示层清洗」与「LLM 注入全文」分离缺失：nanobot 在展示边界清洗、
  保留磁盘/LLM 全文；learn_nano 当前 `_announce` 产出的 content 同时被 LLM 注入与持久化使用，
  没有独立的对外序列化清洗点。这是 step122（独立通道清洗 step）要解决的核心。
- 模板渲染器为最小实现（仅 `{{ var }}`），不支持条件/循环等高级语法；若未来模板变复杂需升级。

## 7. 下一 step 要解决什么

- **独立「通道清洗 step」**：新增 `utils/subagent_channel_display.py`
  （`scrub_subagent_announce_body`，复制 nanobot 逻辑），接入 learn_nano 的消息对外序列化 /
  展示边界，清洗**展示副本**而保留 LLM 全文回放（对齐 nanobot）。
- 后续 **step122（runtime/model 同步 G5）**、**step123（workspace_sandbox + 相位粒度 G9+G10）**、
  **step124（spawn temperature 覆写 G7）** 依次推进。
