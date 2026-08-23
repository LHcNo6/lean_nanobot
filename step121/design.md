# step121 架构设计：子代理 announce 模板化与 origin_message_id 透传

## 1. 总体设计

在现有 `SubagentManager._announce`（step120 已实现 mid-turn 注入与 `session_key_override`）
之上，做两处最小改动：

1. **模板渲染（G6）**：新增 `templates/agent/subagent_announce.md`，并在 `subagent.py` 增加零依赖
   渲染器 `_render_subagent_announce(label, status_text, task, result)`，把 `_announce` 的内联
   f-string 替换为模板渲染。
2. **透传（G8）**：`tools/spawn.py` 在 `origin` 中新增 `origin_message_id`
   （取自 `current_request_context().message_id`）；`_announce` 读取并写入
   `metadata["origin_message_id"]`。

## 2. 关键原理

### 2.1 零依赖模板渲染

learn_nano 无 `render_template` / jinja2。模板文件采用 `{{ var }}` 占位（对齐 nanobot 文件结构），
渲染器用正则 `\{\{\s*(\w+)\s*\}\}` 替换为上下文字典中的值：

```python
_ANNOUNCE_VAR_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")
# 用 _ANNOUNCE_VAR_RE.sub(lambda m: str(ctx.get(m.group(1), "")), template)
```

模板文件经 `Path(__file__).resolve().parent / "templates/agent/subagent_announce.md"` 读取，
结果缓存在模块级 `_ANNOUNCE_TEMPLATE_CACHE`（仅加载一次）。读取失败（如打包缺失）回退内置
`_ANNOUNCE_TEMPLATE_FALLBACK` 常量，保证 announce 始终可渲染。

### 2.2 status_text 对齐

nanobot 用 `"completed successfully"` / `"failed"`；learn_nano 原用 `"completed"` / `"failed"`。
step121 改为 nanobot 同款文案，使模板头部语义一致。

### 2.3 origin_message_id 透传链路

nanobot 中 `SpawnTool` 传 `origin_message_id=request_ctx.message_id`，子代理 `_announce_result`
将其写入元数据。learn_nano 采用同一 `origin` dict 通道（不新增形参），spawn 工具补全
`origin_message_id`；`_announce` 从 `origin` 取之、非空时写入 `metadata`。下游（如 loop）可按需
消费该键（G8 仅要求「透传」）。

### 2.4 为何不在 step121 做通道清洗

`subagent_channel_display`（`scrub_subagent_announce_body`）在 nanobot 的**展示边界**
（session manager / webui）清洗，且**保留 LLM 注入全文**。learn_nano 的 `subagent_result` 经
`_pending_to_user_message` 注入主 agent 上下文（LLM 需看到完整结果），且 `/history` 已用
`HIDDEN_HISTORY_META` 隐藏该行。若在 `_announce` 直接清洗，会同时截断 LLM 注入内容，偏离 nanobot
语义。故推迟到独立 step，在对外序列化边界接入。

## 3. 接口契约（详见 api-spec.md）

- 模板文件 `templates/agent/subagent_announce.md`：`{{ label }} {{ status_text }} {{ task }} {{ result }}`。
- `SubagentManager._announce` 产出 `InboundMessage`：
  - `content` 由模板渲染；
  - `metadata` 含 `injected_event` / `subagent_task_id`，且当 `origin["origin_message_id"]` 非空时
    含 `origin_message_id`。
- `SpawnTool.execute` 的 `origin` 新增 `origin_message_id` 键。

## 4. 测试策略

复用假 bus（记录 `publish_inbound`）与 step117/120 假 runner harness：
- `test_announce_renders_template`：直接调 `_announce`，断言 content 含模板各段。
- `test_announce_origin_message_id_threaded` / `missing`：断言元数据携带 / 缺失 `origin_message_id`。
- `test_spawn_threads_origin_message_id`：端到端经 `spawn` → 假 runner，断言 announce 元数据携带
  `origin_message_id`。
