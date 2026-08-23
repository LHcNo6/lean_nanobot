# step121 接口契约（api-spec）

> 本文件定义 step121「子代理 announce 模板化与 origin_message_id 透传」的对外契约。
> 改动范围：`subagent.py`（渲染 + 元数据）、`tools/spawn.py`（origin 补全）、新增模板文件。

## A. 模板文件契约

`templates/agent/subagent_announce.md`：

```
[Subagent '{{ label }}' {{ status_text }}]

Task: {{ task }}

Result:
{{ result }}

Summarize this naturally for the user. Keep it brief (1-2 sentences). Do not mention technical details like "subagent" or task IDs.
```

- 占位符：`label` / `status_text` / `task` / `result`（均为字符串，缺失时渲染为空串）。
- `status_text` 取值：`"completed successfully"`（ok）/ `"failed"`（error）。

## B. 渲染函数（模块内私有）

```python
def _render_subagent_announce(label: str, status_text: str, task: str, result: str) -> str:
    """用 templates/agent/subagent_announce.md 渲染 announce 文本。"""
```

- 模板加载失败回退 `_ANNOUNCE_TEMPLATE_FALLBACK`（与模板文件内容一致）。

## C. `SubagentManager._announce` 产出契约

`InboundMessage` 字段：

| 字段 | 值 |
| --- | --- |
| `channel` | `"system"` |
| `sender_id` | `"subagent"` |
| `chat_id` / `session_key_override` | `origin.session_key` 或 `f"{channel}:{chat_id}"` |
| `content` | `_render_subagent_announce(...)` 结果 |
| `metadata` | `{"injected_event": "subagent_result", "subagent_task_id": task_id}`；若 `origin["origin_message_id"]` 非空，额外含 `"origin_message_id"` |

## D. `SpawnTool.execute` 契约（增量）

`origin` dict 新增键：

| 键 | 来源 | 缺省 |
| --- | --- | --- |
| `origin_message_id` | `current_request_context().message_id` | `None` |

## E. 不变量

- 既有 mid-turn 注入、`session_key_override` 路由、`injected_event` 标记行为完全不变。
- announce 仍保留 LLM 注入所需的**完整** result 文本（不做通道清洗，清洗推迟独立 step）。
