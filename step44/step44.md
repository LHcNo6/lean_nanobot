# Step 44 — StateTraceEntry 状态追踪

## 解决了什么问题及为什么

当前 `_process_message` 的状态机循环没有任何执行追踪。当 turn 出现性能问题或异常时，无法知道：
- 每个状态（restore/build/run/save/respond）执行了多久
- 状态转换的 event 是什么
- 哪个状态抛出了异常

nanobot 通过 `StateTraceEntry` 记录每个状态的执行轨迹，为调试和性能分析提供可观测性。

### 最小增量范围

只做 3 件事：
1. 新增 `StateTraceEntry` dataclass
2. `TurnContext` 新增 `trace` 字段
3. `_process_message` 循环添加计时和 trace 记录

不做：改变异常处理逻辑、添加日志、trace 序列化/持久化、trace 查询 API。

## 目标和实现

### 目标

- 每个状态执行后记录一条 `StateTraceEntry`（state/started_at/duration_ms/event/error）
- 异常时也记录 trace（error="exception"）
- 不改变现有行为（异常时仍 break，不 raise）

### 实现

#### 1. StateTraceEntry dataclass（loop.py）

```python
@dataclass
class StateTraceEntry:
    """单个状态执行的追踪记录（step44 新增，对齐 nanobot）。"""
    state: TurnState
    started_at: float           # time.perf_counter() 值
    duration_ms: float          # 执行时长（毫秒）
    event: str                  # 状态返回的 event
    error: str | None = None    # 异常时为 "exception"，正常为 None
```

#### 2. TurnContext.trace 字段

```python
@dataclass
class TurnContext:
    # ...
    turn_latency_ms: int | None = None
    # step44：状态执行追踪列表
    trace: list[StateTraceEntry] = field(default_factory=list)
```

#### 3. _process_message 循环

```python
while ctx.state != TurnState.DONE:
    handler_name = f"_state_{ctx.state.name.lower()}"
    handler = getattr(self, handler_name)

    t0 = time.perf_counter()              # 计时开始
    try:
        event = await handler(ctx)
    except Exception as exc:
        duration = (time.perf_counter() - t0) * 1000
        ctx.trace.append(StateTraceEntry(  # 异常 trace
            state=ctx.state, started_at=t0, duration_ms=duration,
            event="", error="exception",
        ))
        ctx.outbound = OutboundMessage(...)  # 保持现有 break 逻辑
        break

    duration = (time.perf_counter() - t0) * 1000
    ctx.trace.append(StateTraceEntry(      # 正常 trace
        state=ctx.state, started_at=t0, duration_ms=duration, event=event,
    ))
    # ... 现有状态转换逻辑不变 ...
```

## 核心函数/类功能说明

### StateTraceEntry
单个状态执行的追踪记录。包含状态名、开始时间（perf_counter）、执行时长（毫秒）、返回 event、异常标记。

### TurnContext.trace
turn 内所有状态执行的追踪列表。每个状态执行后 append 一条，turn 结束后可用于调试和性能分析。

### 计时机制
用 `time.perf_counter()` 高精度计时。`started_at` 是 perf_counter 值（只用于相对计时，不用于绝对时间），`duration_ms` = (结束 - 开始) * 1000。

## 暴露了什么问题

1. **异常处理与 nanobot 不同**：nanobot 异常时记录 trace 然后 raise；step44 异常时记录 trace 然后 break（保持现有行为）。完全对齐需要改变异常传播方式，留到后续。
2. **trace 不可外部访问**：`_process_message` 返回 outbound 而非 ctx，调用方无法直接获取 trace。后续可通过 `process_direct` 返回 ctx 或添加 trace 到 outbound metadata。
3. **无日志输出**：nanobot 有 `logger.debug` 输出每个状态的耗时，step44 未添加。
4. **handler 为 None 未检查**：nanobot 检查 handler 为 None 时 raise RuntimeError，step44 不检查（getattr 会抛 AttributeError，被 except 捕获）。

## 测试

新增 `TestStep44StateTrace` 测试类，7 个测试全部通过：

| 测试 | 验证点 |
|------|--------|
| `test_state_trace_entry_creation` | StateTraceEntry 可正常构造，字段正确 |
| `test_state_trace_entry_error` | error 字段为 "exception" 时 event 为空 |
| `test_turn_context_trace_default_empty` | TurnContext.trace 默认空列表 |
| `test_process_message_records_trace` | 正常 turn 后 trace 非空，每条有 state/duration_ms/event |
| `test_process_message_trace_has_restore_state` | trace 包含 RESTORE 状态 |
| `test_process_message_trace_error_on_exception` | 状态抛异常时 trace 最后一条 error="exception" |
| `test_process_message_trace_duration_positive` | 所有 trace 的 duration_ms >= 0 |

全部测试：438 tests（431 原有 + 7 新增），3 个环境相关失败（与 step43 完全一致），**零回归**。

## 与 nanobot 对齐度

| 维度 | step43 | step44 后 |
|------|--------|----------|
| StateTraceEntry dataclass | ❌ | ✅ |
| TurnContext.trace 字段 | ❌ | ✅ |
| 正常 trace 记录 | ❌ | ✅ |
| 异常 trace 记录 | ❌ | ✅ |
| 异常时 raise（nanobot 行为） | ❌ | ❌（保持现有 break） |
| logger.debug 输出 | ❌ | ❌ |
| trace 持久化/序列化 | ❌ | ❌ |

agent 综合对齐度：~82% → ~83%（A27 部分完成）。

## 下一 step 要解决什么

- **step45**：`_save_turn` 增强 + `_state_command` 持久化——不依赖 step44；
- **step46+**：runner 健壮性对齐——不依赖 step44；
- **后续**：trace 可序列化到 outbound metadata，异常处理对齐 nanobot（raise 而非 break）。
