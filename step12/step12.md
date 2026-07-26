# Step 12 — 流式集成到 AgentLoop

## 目标

把 provider 层的 SSE 流式（Step 2）通过 hook 系统（Step 11）输送到 `AgentRunner._run_loop`，并通过 MessageBus 发布 `StreamDeltaEvent`，让下游消费者（CLI、WebSocket）能逐 chunk 显示回复。

## 改动文件

| 文件 | 变化 |
|------|------|
| `events.py` | 新增 `StreamDeltaEvent(OutboundMessage)` |
| `hook.py` | `AgentHookContext.stream_content`；`AgentHook.on_stream`/`on_stream_end`；`CompositeHook` 同名方法 |
| `runner.py` | `_run_loop` 由 `chat_with_retry` → `chat_stream_with_retry` + `on_content_delta` → `hook.on_stream` |
| `loop.py` | 新增 `StreamPublishingHook(AgentHook)`；`_state_run` 自动注入 |
| `test.py` | mock 重构（`chat_with_retry` → `chat`）；新增 13 个测试，共 58 个 |

## 设计

### 流式调用链

```
AgentRunner._run_loop
  │
  ├─ on_delta(text) — closure inside _run_loop
  │   ├─ iter_ctx.stream_content += text
  │   └─ await hook.on_stream(iter_ctx, text)
  │
  ├─ provider.chat_stream_with_retry(messages, ..., on_content_delta=on_delta)
  │   └─ 每次 chunk → on_delta → hook.on_stream
  │
  ├─ hook.on_stream_end(iter_ctx)            ← 任何 LLM 返回后都触发
  └─ (继续 tool_calls / text 判断流程不变)
```

### StreamPublishingHook

```python
class StreamPublishingHook(AgentHook):
    def __init__(self, bus, chat_id, channel="cli", session_key=None):
        ...

    async def on_stream(self, ctx, delta):
        if not delta:
            return
        await self.bus.publish_outbound(StreamDeltaEvent(content=delta, ...))

    async def on_stream_end(self, ctx):
        await self.bus.publish_outbound(StreamDeltaEvent(finished=True, ...))
```

- 在 `_state_run` 中自动构造并追加到 hook 链末尾
- 空 delta 跳过（不发布）

### StreamDeltaEvent

```python
@dataclass
class StreamDeltaEvent(OutboundMessage):
    finished: bool = False
    session_key: str | None = None
```

继承 `OutboundMessage`，复用 `content`/`channel`/`chat_id`。消费者通过 `isinstance(msg, StreamDeltaEvent)` 区分。

### 总线消息时序

```
[StreamDeltaEvent("Hel")    , finished=False]
[StreamDeltaEvent("lo ")    , finished=False]
[StreamDeltaEvent("world")  , finished=False]
[StreamDeltaEvent("!" )     , finished=False]
[StreamDeltaEvent("" )      , finished=True ]
[OutboundMessage("Hello world!")           ]
```

消费者先收到逐 chunk 的 delta，最后收到 `finished=True` 标记，然后收到完整的 `OutboundMessage`。

## 测试

13 个新测试（58 个总计）：

| 测试 | 验证 |
|------|------|
| `test_on_stream_called_with_deltas` | `on_stream` 收到 `["Hello"," ","world","!"]` |
| `test_stream_content_accumulated` | `ctx.stream_content == "Hello world!"` |
| `test_on_stream_end_called` | `on_stream_end` 被调用 |
| `test_no_stream_when_tool_calls` | 工具迭代不触发流式 delta |
| `test_stream_usage_accumulated` | 流式下 token 用量累计 |
| `test_publishes_deltas_to_bus` | `StreamPublishingHook` 每条 delta → `StreamDeltaEvent` |
| `test_publishes_finished_signal` | `on_stream_end` → `StreamDeltaEvent(finished=True)` |
| `test_skip_empty_delta` | 空 delta 不发布 |
| `test_loop_streaming_end_to_end` | AgentLoop 全链路流式 |
| `test_loop_streaming_with_hooks` | 流式 + 其他 hook 共存 |

## 与 nanobot 对齐

```
nanobot/agent/hook.py → step12/hook.py (~75% 对齐)
  - on_stream / on_stream_end 加入 AgentHook
  - CompositeHook 含错误隔离的 fanout

nanobot/agent/runner.py → step12/runner.py (~60% 对齐)
  - 始终使用 chat_stream_with_retry
  - on_content_delta 路由到 hook.on_stream

nanobot/bus/events.py → step12/events.py
  - StreamDelta 等价于 StreamDeltaEvent

nanobot/agent/loop.py → step12/loop.py
  - StreamPublishingHook 自动注入
```

## 下一站

Step 13 — 回合中注入（Mid-turn injection）。
