# Step 10 — AgentLoop 状态机

## 目标

将 step9 中内联在 `main.py` 的 `_agent_loop` 重构为正式的 `AgentLoop` 类，引入 6 态状态机，实现 per-session 序列化和跨 session 并发。

## 解决的问题

| 问题 | 解决方式 |
|------|---------|
| `_agent_loop` 不可复用 | 提取为独立 `loop.py`，`AgentLoop` 类 |
| 无结构化状态 | 6 态状态机 + 转换表 `_TRANSITIONS` |
| 同 session 竞态 | `asyncio.Lock` 每个 session_key |
| 跨 session 并发 | `asyncio.create_task` 每个消息 |
| 错误导致进程死 | 每个状态 handler 的异常被 `_process_message` 捕获并返回 error outbound |
| 命令与处理耦合 | 命令在 CLI 层处理，状态机纯粹 |

## 新增文件

| 文件 | 行数 | 职责 |
|------|------|------|
| `loop.py` | 150 | `AgentLoop` 类 + `TurnState` 枚举 + `TurnContext` 数据类 |

## 修改文件

| 文件 | 变化 |
|------|------|
| `main.py` | 使用 `AgentLoop(bus, ...)` 代替内联 `_agent_loop` |
| `test.py` | 增加 14 个 AgentLoop 测试，共 44 个测试 |

## 6 态状态机

```
RESTORE → COMPACT → BUILD → RUN → SAVE → RESPOND → DONE
```

### 转换表

```python
_TRANSITIONS = {
    (TurnState.RESTORE, "ok"): TurnState.COMPACT,
    (TurnState.COMPACT, "ok"): TurnState.BUILD,
    (TurnState.BUILD,   "ok"): TurnState.RUN,
    (TurnState.RUN,     "ok"): TurnState.SAVE,
    (TurnState.SAVE,    "ok"): TurnState.RESPOND,
    (TurnState.RESPOND, "ok"): TurnState.DONE,
}
```

### 状态职责

| 状态 | Handler | 操作 |
|------|---------|------|
| RESTORE | `_state_restore` | `sessions.get_or_create(session_key)` |
| COMPACT | `_state_compact` | `consolidator.maybe_consolidate(session, max_tokens)` |
| BUILD | `_state_build` | `session.get_history()` + `context.build_messages()` |
| RUN | `_state_run` | `AgentRunner().run(spec)` |
| SAVE | `_state_save` | `session.import_messages()` + `sessions.save()` |
| RESPOND | `_state_respond` | `bus.publish_outbound(OutboundMessage(...))` |

### TurnContext

```python
@dataclass
class TurnContext:
    msg: InboundMessage
    session_key: str
    state: TurnState = TurnState.RESTORE
    session: Session | None = None
    summary: str | None = None
    history: list[dict] = field(default_factory=list)
    initial_messages: list[dict] = field(default_factory=list)
    result: AgentRunResult | None = None
    outbound: OutboundMessage | None = None
```

## 核心流程

### `run()` — 主循环

```python
async def run(self):
    self.running = True
    while self.running:
        msg = await self.bus.consume_inbound()
        asyncio.create_task(self._dispatch(msg))
```

### `_dispatch(msg)` — 分发

```python
async def _dispatch(self, msg):
    session_key = msg.session_key or msg.chat_id
    lock = self._session_locks.setdefault(session_key, asyncio.Lock())
    async with lock:           # 同 session 串行
        response = await self._process_message(msg, session_key)
        if response is not None:
            await self.bus.publish_outbound(response)
```

### `_process_message(msg)` — 状态引擎

```python
async def _process_message(self, msg, session_key):
    ctx = TurnContext(msg=msg, session_key=session_key)
    while ctx.state != TurnState.DONE:
        handler = getattr(self, f"_state_{ctx.state.name.lower()}")
        try:
            event = await handler(ctx)
        except Exception as exc:
            # 错误状态 → 返回 error outbound
            ctx.outbound = OutboundMessage(content=f"Error: {exc}", ...)
            break
        next_state = self._TRANSITIONS[(ctx.state, event)]
        ctx.state = next_state
    return ctx.outbound
```

## 与 step9 的架构对比

```
step9 (_agent_loop inline):       step10 (AgentLoop):
─────────────────────────────     ────────────────────────────
函数，不可复用                    类，可独立导入/测试
扁平顺序逻辑                      6态状态机
无 session 锁                     Per-session asyncio.Lock
单线程串行                         跨 session 并发
异常直接崩掉                      异常被捕获 → error outbound
命令通过 metadata hack             命令在 CLI 层处理
```

## 测试

14 个新测试（44 个总计）：

| 测试 | 内容 |
|------|------|
| `test_state_restore` | RESTORE 创建/加载 Session |
| `test_state_compact_noop` | COMPACT 无压缩时 noop |
| `test_state_compact_with_summary` | COMPACT 生成摘要 |
| `test_state_build` | BUILD 组装 history + messages |
| `test_state_run` | RUN 运行 AgentRunner |
| `test_state_save` | SAVE 持久化消息 |
| `test_state_respond` | RESPOND 发布 OutboundMessage |
| `test_state_transitions` | 验证完整转换链 |
| `test_error_in_state_caught_by_process_message` | 异常被捕获为 error outbound |
| `test_full_turn` | 完整一轮 RESTORE→...→DONE |
| `test_full_turn_with_history` | 多轮消息历史持久化 |
| `test_per_session_lock` | 同 session 串行 |
| `test_cross_session_concurrent` | 不同 session 并行 |
| `test_loop_stop_exits` | stop() 退出循环 |
| `test_agent_roundtrip_via_loop` | 总线往返完整测试 |

## 关键设计决策

| 决策 | 选择 | 理由 |
|------|------|------|
| 状态数 | 6 态（无量 COMMAND 态） | 保持最小增量，对齐 roadmap |
| 命令处理位置 | CLI 层 | 状态机保持纯粹，不混入命令路由 |
| 错误处理 | try/except → error outbound | 简单恢复不崩 |
| Session 锁 | `asyncio.Lock()` per key | 防止同一 session 并发写入 |
| 跨 session 并发 | 无上限 | 后续可加 Semaphore |

## 与 nanobot 对齐

```
nanobot/agent/loop.py → step10/loop.py (60% 对齐)
  相同: 状态枚举 + 转换表 + per-session lock + _dispatch + stop()
  简化: 无 COMMAND 态、无 Semaphore、无 streaming、无 pending_queue、无 runtime events
  未来: step13 mid-turn injection, step12 streaming, step11 hooks
```

## 下一站

Step 11 — Hook 系统：AgentRun 生命周期钩子（before_run, after_run, on_error, on_stream）。
