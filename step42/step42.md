# Step 42 — `_assemble_outbound` 提取 + MessageTool 抑制（最小增量版）

## 解决了什么问题及为什么

step41 的 `_state_respond` 内联构建 OutboundMessage，存在两个对齐缺口：

1. **逻辑耦合**：出站消息组装逻辑与 state handler 耦合，无法复用和单独测试。nanobot 将其提取为独立方法 `_assemble_outbound`，供 `_state_respond` 和未来的 `process_direct` 共用。
2. **无 MessageTool 抑制**：当 LLM 通过 `message` 工具直接发送消息后，loop 仍会重复出站，导致用户收到两条消息。nanobot 通过 `mt._sent_in_turn` 标记抑制。

### 最小增量范围

本 step 只做 3 件事，不做过度对齐：

| 做 | 不做（留到后续） |
|----|-----------------|
| 提取 `_assemble_outbound` 方法 | 不移除 meta 中的 stop_reason/tokens |
| 实现极简 MessageTool + 抑制逻辑 | 不用 ContextVar（简单 bool 即可） |
| 新增 `latency_ms` 到 meta | 不继承 msg.metadata / msg.channel/chat_id |
| | 不实现 MessageTool 的 media/buttons/跨通道 |

## 目标和实现

### 目标

1. 从 `_state_respond` 提取 `_assemble_outbound` 独立方法；
2. 实现极简 MessageTool（`tools/message.py`），支持 `_sent_in_turn` 标记和 `start_turn()`；
3. `_state_build` 调用 `message_tool.start_turn()`；
4. `_assemble_outbound` 中加入 MessageTool 抑制逻辑；
5. `turn_latency_ms` 从 `_state_save` 透传到 `_assemble_outbound`，meta 新增 `latency_ms`。

### 实现

#### 1. TurnContext 新增 `turn_latency_ms` 字段（loop.py）

```python
@dataclass
class TurnContext:
    # ...
    turn_latency_ms: int | None = None  # step42 新增
```

#### 2. `_state_save` 存入 `ctx.turn_latency_ms`

```python
latency_ms = (...)
ctx.turn_latency_ms = latency_ms  # step42 新增
self._runtime_events().record_turn_latency(ctx.session_key, latency_ms)
```

#### 3. 极简 MessageTool（`tools/message.py`，新建）

```python
@tool_parameters(tool_parameters_schema(
    content=StringSchema("Message content to send proactively."),
    required=["content"],
))
class MessageTool(Tool):
    def __init__(self, send_callback=None):
        self._send_callback = send_callback
        self._sent_in_turn = False  # 简单 bool，不用 ContextVar

    @classmethod
    def create(cls, ctx):
        return cls(ctx.bus.publish_outbound if ctx.bus else None)

    def start_turn(self):
        self._sent_in_turn = False

    @property
    def name(self): return "message"
    @property
    def description(self): return "Proactively send a message..."

    async def execute(self, content="", **kw):
        from step42.bus.events import OutboundMessage
        if not self._send_callback:
            return ToolResult.error("Error: Message sending not configured")
        await self._send_callback(OutboundMessage(content=content))
        self._sent_in_turn = True
        return "Message sent"
```

ToolLoader 自动扫描 `tools/` 目录加载，无需手动注册。

#### 4. `_state_build` 调用 `start_turn()`

```python
# step42：MessageTool 每个 turn 开始时重置 _sent_in_turn
from step42.tools.message import MessageTool
if message_tool := self.registry.get("message"):
    if isinstance(message_tool, MessageTool):
        message_tool.start_turn()
```

> 注意：step41/42 用 `self.registry`（不是 nanobot 的 `self.tools`）。

#### 5. 提取 `_assemble_outbound` 方法

```python
def _assemble_outbound(
    self, msg, final_content, all_msgs, stop_reason,
    had_injections, on_stream, *, turn_latency_ms=None,
) -> OutboundMessage | None:
    # step42：MessageTool 抑制
    from step42.tools.message import MessageTool
    if (mt := self.registry.get("message")) and isinstance(mt, MessageTool) and mt._sent_in_turn:
        if not had_injections or stop_reason == "empty_final_response":
            return None

    meta = {"stop_reason": stop_reason, "tokens": ""}
    event = None
    if on_stream is not None and stop_reason not in {"error", "tool_error"}:
        event = StreamedResponseEvent()
    if turn_latency_ms is not None:
        meta["latency_ms"] = int(turn_latency_ms)
    return OutboundMessage(content=final_content, metadata=meta, event=event)
```

#### 6. `_state_respond` 简化

```python
async def _state_respond(self, ctx):
    if ctx.suppress_response:
        ctx.outbound = None
        return "ok"
    if ctx.result is None:
        ctx.outbound = OutboundMessage(content="", metadata={"stop_reason": "empty"})
        return "ok"
    outbound = self._assemble_outbound(
        ctx.msg, ctx.result.final_content or "", ctx.all_messages,
        ctx.result.stop_reason, ctx.had_injections, ctx.on_stream,
        turn_latency_ms=ctx.turn_latency_ms,
    )
    # tokens 在调用方填充（_assemble_outbound 不持有 ctx.result）
    if outbound is not None:
        outbound.metadata["tokens"] = f"{ctx.result.total_prompt_tokens}+{ctx.result.total_completion_tokens}"
    ctx.outbound = outbound
    if ctx.ephemeral and ctx.outbound is not None:
        ctx.outbound.metadata["_stop_reason"] = ctx.stop_reason
    return "ok"
```

## 核心函数/类功能说明

### `_assemble_outbound`
从 `_state_respond` 提取的独立方法，组装最终出站消息。支持 MessageTool 抑制（返回 None）和 latency_ms 元数据。最小增量版保留 stop_reason/tokens 字段。

### `MessageTool`
极简主动消息发送工具。LLM 调用后直接发送消息并标记 `_sent_in_turn=True`，`_assemble_outbound` 检测到该标记后抑制重复出站。

### `MessageTool.start_turn()`
每个 turn 开始时重置 `_sent_in_turn=False`，由 `_state_build` 调用。

### `TurnContext.turn_latency_ms`
turn 延迟（毫秒），`_state_save` 计算并存入，`_state_respond` 传递给 `_assemble_outbound`，最终存入 outbound.metadata。

## 暴露了什么问题

1. **tokens 字段填充位置不优雅**：`_assemble_outbound` 不持有 `ctx.result`，所以 tokens 在 `_state_respond` 中填充。后续完全对齐 nanobot 时移除 tokens 字段即可解决。
2. **MessageTool 用简单 bool 而非 ContextVar**：step42 单 turn 顺序执行没问题，但未来支持并发 turn 时需要改为 ContextVar（对齐 nanobot）。
3. **meta 未完全对齐 nanobot**：保留了 stop_reason/tokens，未继承 msg.metadata，未继承 msg.channel/chat_id。这些留到后续清理 step。
4. **MessageTool 极简版不区分通道**：只要发送就标记 `_sent_in_turn=True`，不区分是否当前通道。nanobot 只在发送到当前默认通道时标记。跨通道场景留到后续。

## 测试

新增 3 个测试类，15 个测试全部通过：

| 测试类 | 测试数 | 覆盖 |
|--------|--------|------|
| `TestStep42AssembleOutbound` | 8 | 基本组装、latency_ms、stream event、error 无 event、MessageTool 抑制、有注入不抑制、empty_final_response 抑制、无 MessageTool |
| `TestStep42MessageTool` | 4 | start_turn 重置、execute 标记、无 callback 错误、name 属性 |
| `TestStep42Integration` | 2 | _state_build 调 start_turn、_state_save 存 turn_latency_ms |

全部测试：421 tests（406 原有 + 15 新增），3 个环境相关失败（与 step41 完全一致），**零回归**。

## 与 nanobot 对齐度

| 维度 | step41 | step42 后 |
|------|--------|----------|
| `_assemble_outbound` 独立方法 | ❌ | ✅ |
| MessageTool 最小实现 | ❌ | ✅ |
| MessageTool _sent_in_turn 抑制 | ❌ | ✅ |
| _state_build 调 start_turn | ❌ | ✅ |
| meta latency_ms | ❌ | ✅ |
| meta 完全对齐（无 stop_reason/tokens） | ❌ | ❌（后续） |
| MessageTool ContextVar | ❌ | ❌（后续） |
| MessageTool media/buttons/跨通道 | ❌ | ❌（后续） |

agent 综合对齐度：~80% → ~81%（A25 部分完成）。

## 下一 step 要解决什么

- **step43**：`process_direct` 公共 API——依赖 step41（ephemeral）和 step42（`_assemble_outbound`），新增 `process_direct(prompt, session_key, ephemeral, hooks, hook_factories, tools, persist_user_message, runtime)`，绕过 bus 直接走状态机，`run_dream` 标记 deprecated；
- **后续**：meta 完全对齐 nanobot（移除 stop_reason/tokens、继承 msg.metadata）、MessageTool 升级为 ContextVar + 跨通道 + media/buttons。
