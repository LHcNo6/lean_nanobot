# Step 13 — 回合中注入 (Mid-turn injection)

## 目标

允许**正在执行的 agent turn** 被外部消息中断并注入上下文。用户在当前 turn 处理期间发来的消息不再简单排队等下一轮，而是被注入到当前 turn 内，让 LLM 即时看到并响应。

## 改动文件

| 文件 | 变化 |
|------|------|
| `loop.py` | `_pending_queues` 字典；`_get_or_create_queue` 懒创建队列；`_dispatch` 加锁检测 + 排队；`_drain_leftover` 残留回捞；`_state_run` 创建 `injection_callback` 闭包 |
| `runner.py` | `AgentRunSpec.injection_callback` 字段；`_run_loop` 两个注入点（工具执行后 + 文本响应前） |
| `test.py` | 新增 12 个测试，共 70 个 |

## 设计

### 核心数据流

```
用户发 "hello" → InboundMessage → AgentLoop._dispatch
  → 锁空闲 → 加锁 → _process_message → RUN
  → AgentRunner.run(spec) with injection_callback
    → _run_loop:
      → 迭代 0: LLM → tool_calls → 执行工具
        → 【注入点 1】injection_callback() → drain 队列
          → 若命中 → 追加到 messages → continue
      → 迭代 1: LLM → 文本 "Hello!"
        → 【注入点 2】injection_callback() → drain 队列
          → 若命中 → 追加 → continue
          → 若空 → 返回 result
  → SAVE → RESPOND → OutboundMessage → 锁释放

用户中途发 "tell me more"
  → _dispatch → 锁被持有 → put 到 _pending_queues[session]
  → 下一注入点 drain → 注入到当前 turn
```

### _pending_queues

```python
self._pending_queues: dict[str, asyncio.Queue[InboundMessage]] = {}
```

- Key: `session_key`，与 `_session_locks` 一致
- 每个队列 maxsize=20，提供背压
- 懒创建（`_get_or_create_queue`）

### _dispatch 排队逻辑

```python
async def _dispatch(self, msg):
    session_key = msg.session_key or msg.chat_id
    lock = self._session_locks.setdefault(session_key, asyncio.Lock())
    if lock.locked():
        await self._get_or_create_queue(session_key).put(msg)
        return
    async with lock:
        response = await self._process_message(msg, session_key)
        if response is not None:
            await self.bus.publish_outbound(response)
    await self._drain_leftover(session_key)
```

- `lock.locked()` 检查是**尽力而为**的竞态检测
- 如果竞态导致锁刚被释放，消息走正常 turn（不注入但也不丢失）

### injection_callback

在 `_state_run` 中构造闭包，捕获 `ctx.session_key`：

```python
async def injection_callback() -> list[dict]:
    queue = self._pending_queues.get(ctx.session_key)
    if queue is None or queue.empty():
        return []
    msgs = []
    while not queue.empty():
        try:
            m = queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        msgs.append({"role": "user", "content": m.content})
    return msgs
```

- 一次性 drain **所有**排队消息
- 返回 `[{role, content}]` 格式

### Runner 两个注入点

**注入点 1**（工具执行后，runner.py:129-132）：

```python
if spec.injection_callback:
    injected = await spec.injection_callback()
    for msg in injected:
        messages.append(msg)
continue
```

**注入点 2**（文本响应前，runner.py:140-145）：

```python
if spec.injection_callback:
    injected = await spec.injection_callback()
    if injected:
        for msg in injected:
            messages.append(msg)
        continue

return AgentRunResult(...)
```

- 注入点 1 无条件的（反正要 `continue`）
- 注入点 2 有条件：只有命中注入消息时才 `continue`，否则正常 `return`
- `continue` 会让迭代计数器增加，`max_iterations` 限制总迭代次数

### 残留消息回捞

```python
async def _drain_leftover(self, session_key: str) -> None:
    queue = self._pending_queues.get(session_key)
    if queue and not queue.empty():
        try:
            msg = queue.get_nowait()
        except asyncio.QueueEmpty:
            return
        await self.bus.publish_inbound(msg)
```

- 在 `_dispatch` 的 `async with lock` 块后调用
- 处理**最后一次 injection_callback 之后、_process_message 返回之前**到达的消息
- 重新 `publish_inbound` 到总线，`run()` 主循环会消费并调度新 turn
- 如果有残留消息在 pending 队列但还没来得及被 `_dispatch` 调用 `_drain_leftover`，下一个 `_dispatch` 的锁检测会排队它

## 测试（12 个新增）

| 测试 | 验证 |
|------|------|
| `test_injection_callback_returns_messages` | Callback drain 返回 `[{role, content}]` |
| `test_runner_injection_after_tool_execution` | 工具执行后注入消息出现在对话中 |
| `test_runner_injection_before_final_response` | 文本响应前注入，turn 被延长 |
| `test_runner_injection_extends_turn` | 注入后 LLM 多轮迭代 |
| `test_empty_injection_callback_noop` | 空 callback 不影响流程 |
| `test_injection_preserves_assistant_message` | 注入延长时，已有 assistant 消息保留 |
| `test_no_injection_callback_works` | 无 callback 时正常运行 |
| `test_injection_callback_single_call_multiple_messages` | 多条消息一次性 drain |
| `test_loop_get_or_create_queue` | 队列创建与缓存 |
| `test_loop_state_run_creates_injection_callback` | `_state_run` 创建 callback |
| `test_leftover_drain_republishes_to_bus` | 残留消息回捞到 bus.inbound |
| `test_leftover_drain_empty_noop` | 空队列 drain 无操作 |

## 与 nanobot 对齐

```
nanobot/agent/loop.py → step13/loop.py
  - _pending_queues[session_key] = Queue(maxsize=20)
  - _dispatch 排队 + _drain_leftover

nanobot/agent/runner.py → step13/runner.py
  - injection_callback on AgentRunSpec
  - 工具执行后 + 文本响应前 drain
```

## 下一站

Step 14 — Context Governance
