# Step 26 — Data Flow (Event Layer: typed outbound events + RuntimeEventBus)

step26 新增/变更的链条：① typed 出站事件链（生产者 → 工厂 → 消息总线 → manager 类型路由 → 通道渲染）② 运行时事件链（loop 生命周期点 → RuntimeEventPublisher → RuntimeEventBus → 订阅者）③ 重试心跳链（provider 30s 分段 sleep → on_retry_wait → RetryWaitEvent）④ 工具进度链（runner → progress 回调 → ProgressEvent）⑤ 流式最终标记链（wants_streaming → StreamedResponseEvent）。回合状态机 8 态、装配链、provider 回退链（step21/22/23/25 成果）零改动。

---

## 图 0：全局视图（双总线 + 五条链）

```
   ┌─────────────────── 回合编排链 (step21 原样，8 态) ────────────────────┐
   │ RESTORE→COMPACT→COMMAND→BUILD→RUN→SAVE→RESPOND→DONE                    │
   │                                                                        │
 ①typed出站事件链(消息总线)       ②运行时事件链(进程内总线)     ③④⑤生产者
 runner._emit_tool_progress   _state_restore:            provider._sleep_with_heartbeat
  → spec.progress_callback     session_turn_started(409)  → on_retry_wait(回调)
  → build_bus_progress_cb      _state_run:                runner._emit_tool_progress
  → ProgressEvent               run_status running(688)   loop._state_respond
 provider 心跳                  record_turn_runtime(691)   StreamedResponseEvent(758)
  → on_retry_wait → RetryWait  _state_save:               │
  loop._state_respond           record_turn_latency(724)   ▼
  StreamedResponseEvent(758)   finally:                   outbound_message_for_event(83)
    │                           turn_completed(339)         │
    ▼                           idle(345) + clear_turn(348) ▼
 outbound_message_for_event     │                       MessageBus.outbound
    ▼                           ▼                       manager._dispatch_outbound(123)
 MessageBus.outbound     RuntimeEventBus.publish(106)      ├─ StreamEndEvent→send_delta
 manager 路由(142-157)          │                          ├─ Progress/RetryWait→门控send
    ├─ send_delta(stream_end)  订阅者:                     └─ 其余→send(退避重试)
    ├─ 门控 send               main.py:109(演示)           │
    └─ send                 未来 WebUI/状态机观测           ▼
    ▼                                                    CliChannel.send(62-75)
 CliChannel: typed 事件不结束 turn，最终消息才 _turn_done.set()
```

---

## 图 1：双总线架构（★全新，本 step 的心智模型）

```
消息总线 MessageBus (bus/queue.py)          运行时事件总线 RuntimeEventBus (bus/runtime_events.py:78)
   ┌─ inbound:  通道 → loop                    订阅者可选（WebUI/CLI/状态机观测）
   └─ outbound: loop/runner/provider → 通道    事件: SessionTurnStarted / TurnRunStatusChanged /
      OutboundMessage.event 携带 typed 事件      TurnCompleted（RuntimeEventContext 包裹）
      （progress / retry_wait / stream_end）   publish: await 异步 handler，严格跟随用户消息排布(106)
                                              publish_nowait: 无 loop 则丢弃+debug(121)
  职责: 用户/聊天交付（通道需要 chat 路由字段）   职责: 进程内状态通知（与交付解耦）
  ★决策: 不另建事件队列——typed 事件只是消息的    ★决策: 独立总线让"观测"不污染"交付"；
  一个字段，通道路由语义不变                    订阅者挂接零成本（main.py:109 三行演示）
```

---

## 图 2：typed 出站事件链（★全新，生产者 → 通道）

```
生产者                                                        bus/outbound_events.py
 ③ provider 重试心跳 ──► on_retry_wait ──► loop._build_retry_wait_callback (loop.py:279)
 ④ runner 工具进度   ──► spec.progress_callback ──► build_bus_progress_callback (bus/progress.py:18)
 ⑤ loop 流式最终     ──► _state_respond (loop.py:743-763)
        │                                                          │
        ▼                                                          ▼
  outbound_message_for_event(channel, chat_id, event,           6 个 frozen dataclass:
        content=None, metadata=None)  (outbound_events.py:83)     ProgressEvent(30) / RetryWaitEvent(45)
        │ content 缺省时 _event_content(106) 推导                   StreamEndEvent(52) / StreamedResponseEvent(61)
        ▼ （Progress/RetryWait/StreamEnd 取自身内容，其余 "")）        TurnEndEvent(68) / GoalStatusEvent(76)
  OutboundMessage(channel, chat_id, content, metadata,           ★后两个只定义类型，本 step 无人产出
        event=...)  ← 消息模型新增 event 字段 (bus/events.py:47)  （真实通道收到最终响应后才生成）
        ▼
  bus.publish_outbound → MessageBus.outbound 队列
        ▼
  manager._dispatch_outbound 类型路由（图 3）
```

---

## 图 3：manager 事件路由决策树（★升级，manager.py:123-161）

```
consume_outbound() → msg
   │ msg.channel 无对应通道 → [manager] Unknown channel
   ▼
   ├─ isinstance(msg, StreamDeltaEvent)          (132-141) legacy 兼容（step25 形态）
   │     ├─ finished=True → send_delta("", stream_end=True)
   │     └─ 否则 → send_delta(content)
   ├─ isinstance(msg.event, StreamEndEvent)      (142-151) ★typed 流式结束
   │     └─ send_delta(content, stream_id, stream_end=True, resuming)
   ├─ isinstance(msg.event, (ProgressEvent, RetryWaitEvent))   (152-155) ★门控
   │     └─ event.content 非空才 _send_with_retry（通道决定是否展示）
   └─ 其余（含 StreamedResponseEvent 最终消息）  (156-157)
         └─ _send_with_retry（1s/2s/4s 退避重试）
★测试锚点: TestManagerEventRouting.test_stream_end_routes_to_send_delta (test_events.py)
          TestManagerEventRouting.test_progress_gated_by_content
```

---

## 图 4：重试心跳链（★全新，provider.py:40-75）

```
provider.chat_with_retry (provider.py:99) / chat_stream_with_retry (131)
   │ 瞬态异常 → delay = _backoff_delay(attempt)
   ▼
_sleep_with_heartbeat(delay, attempt, on_retry_wait)          (provider.py:43)
   │ _RETRY_HEARTBEAT_CHUNK = 30.0  长退避按 30s 分段          (40)
   │ while remaining > 0:
   │    on_retry_wait("Model request failed, retry in Xs (attempt N).")
   │    sleep(min(remaining, 30s)) ★分段期间持续上报，UI 不静默
   ▼
on_retry_wait 来源二选一:
   ├─ loop._build_retry_wait_callback (loop.py:279-298)       ──► RetryWaitEvent → 图 2 → 图 3
   │     （默认装配：经消息总线出站，cli 打印 "  · ..." 状态行）
   └─ 签名探测兼容（★关键决策）:
        runner._provider_method_accepts (runner.py:196) 只在 provider 声明
          on_retry_wait / **kwargs 时才传（防 388 回归窄签名 mock TypeError）
        fallback_provider._filter_kwargs (fallback_provider.py:226) 按目标方法
          签名过滤 kwargs——mock 与真实 provider 签名宽度不一致的通用解法
★测试锚点: TestProviderRetryWaitHeartbeat.test_chat_with_retry_emits_heartbeat
          （fail_count=1 + base_delay=0.001 真实短退避 + 断言 waits 列表）
★对照 nanobot: providers/base.py:_sleep_with_heartbeat（同一分段语义）
```

---

## 图 5：工具进度链（★全新，runner.py:289, 294-318）

```
runner._run_loop 工具批量执行
   │ result = spec.tools.execute(name, **args)   (runner.py:288)
   ▼
_emit_tool_progress(spec, name, result)          (runner.py:294-318)
   │ spec.progress_callback is None → return（未装配则零开销）
   │ content = f"Ran tool {name}: {snippet}"（结果截断 80 字符，低噪声）
   │ inspect.signature 探测:
   │   ├─ 有 content 参数 / **kwargs → callback(content)
   │   └─ 旧式窄签名回调 → callback()  ★复用 step23 的签名探测风格
   ▼
spec.progress_callback ← loop 装配的 build_bus_progress_callback(bus, msg)
   （bus/progress.py:18：闭包持有 channel/chat_id/metadata）
   ▼
ProgressEvent(content, tool_hint=False, ...) → 图 2 → 图 3 门控 → cli 状态行
★tool_hint / reasoning* / file_edit_events 字段已定义但本 step 不置位（step30 hook 体系）
★测试锚点: TestLoopProgressOutbound.test_tool_run_publishes_progress_event（echo 工具 + 消费 outbound）
```

---

## 图 6：运行时事件链 — turn 生命周期时序（★核心，loop.py 五个挂点）

```
状态机 turn                                                    RuntimeEventPublisher
                                                                (runtime_events.py:135)
 _state_restore (409)
   session_turn_started(msg, key) ────────────────► SessionTurnStarted  (180)
 _state_run (688-691)  ★惰性装配 progress/retry_wait 回调
   run_status_changed(running, started_at) ───────► TurnRunStatusChanged (198)
   record_turn_runtime(key, self.runtime) ────────► 暂存 _turn_runtime   (163)
   runner.run(spec)（内部: RetryWaitEvent/ProgressEvent → 消息总线，图 4/5）
   hook.wants_streaming() → ctx.on_stream = hook   (705-706)
 _state_save (724)
   record_turn_latency(key, latency_ms) ──────────► 暂存 _turn_latency_ms (168)
 _state_respond (754-758)
   流式且 stop_reason ∉ {error, tool_error} → 挂 StreamedResponseEvent → 图 7
 finally (339-348) ★无论成败
   turn_completed(channel, chat_id, key, metadata)─► TurnCompleted (221)
        └─ 弹出暂存 latency/runtime 一并派发 ★"取出即删"
   run_status_changed(idle) ─────────────────────► TurnRunStatusChanged
   clear_turn(key) ──────────────────────────────► 清暂存 (174)
        ▼
   RuntimeEventBus.publish (106) 按注册顺序 await 每个匹配订阅者
        ├─ handler 异常 → 吞掉记日志，不影响其它订阅者（异常隔离）
        └─ 订阅者: main.py:109 演示（打印 [runtime] 三行）; 未来 WebUI
★测试锚点: TestLoopRuntimeEvents.test_full_turn_emits_lifecycle_events
           （started→running→completed latency≠None→idle 全序断言）
★对照 nanobot: loop._state_* 生命周期事件同点同义
```

---

## 图 7：流式最终标记链（★loop.py:705-706, 754-758）

```
hook.wants_streaming() ──真──► ctx.on_stream = spec.hook   (705-706)
        │
        ▼ _state_respond (743-763)
   event = None
   if ctx.on_stream is not None 且 stop_reason ∉ {error, tool_error}:
        event = StreamedResponseEvent()             (758)
   OutboundMessage(content=final, metadata={stop_reason, tokens}, event=event)
        ▼
   manager 普通 send 分支 (156-157) → CliChannel.send (cli.py:62-75)
        ├─ event 是 Progress/RetryWait? → 状态行打印 + return（不结束 turn）(65-68)
        └─ 最终消息 → 打印 [stop_reason]/content/tokens → _turn_done.set() (75)
★设计: 通道需要知道"内容已经以 delta 流过"（对齐 nanobot _state_respond 的
  on_stream 判定）——StreamedResponseEvent 是零字段的语义标记
★测试锚点: TestLoopRuntimeEvents.test_final_response_carries_streamed_event
          TestCliChannelEventSemantics（typed 事件不结束 turn / 最终消息才 set）
```

---

## 图 8：事件类型路由总表（★对照）

```
类型                     字段(默认)                 生产者                消费者行为
ProgressEvent(30)       content/tool_hint/reasoning* runner 工具进度       manager 门控 send → cli 状态行
RetryWaitEvent(45)      content                     provider 重试心跳      manager 门控 send → cli 状态行
StreamEndEvent(52)      content/stream_id/resuming  未来流式通道            manager → send_delta(stream_end)
StreamedResponseEvent(61) —（纯标记）                 loop._state_respond   manager 普通 send → cli 结束 turn
TurnEndEvent(68)        latency_ms/goal_state        无（仅定义类型）      —
GoalStatusEvent(76)     status/started_at            无（仅定义类型）      —
SessionTurnStarted(41)  context                      loop 409              RuntimeEventBus 订阅者
TurnRunStatusChanged(49) context/status/started_at   loop 345/688          RuntimeEventBus 订阅者
TurnCompleted(59)       context/latency/runtime      loop 339              RuntimeEventBus 订阅者
legacy StreamDeltaEvent —                           step25 流式路径         manager 132-141 兼容分支
```

---

## 图 9：阅读路径建议

```
图 1 (双总线)      ← 先建立心智模型：消息总线管交付、运行时总线管状态观测
   │
   ▼ 交付侧
图 2 (出站事件链)   ← OutboundMessage.event 字段是本次一切的地基（events.py:47）
图 3 (manager 路由) ← typed 事件如何被分流（send vs send_delta vs 门控）
   │
   ▼ 三个生产者
图 4 (重试心跳)     ← provider 分段 sleep + 签名探测兼容（跨 step 主题）
图 5 (工具进度)     ← runner 低噪声进度 + 回调签名探测
图 7 (流式标记)     ← 最终消息怎么告知通道"内容已流过"
   │
   ▼ 观测侧
图 6 (生命周期)     ← loop 五个挂点 → Publisher 暂存/弹出 → Bus → 订阅者
   │
   ▼
图 8 (路由总表)     ← 6+3+1 事件一次看全
核心一句话：step26 把"运行时/UI 语义"从 metadata 魔法 flag 升级为类型化事件，
并在消息总线旁加了第二条进程内总线专供状态观测；事件的生产（loop/provider/
runner）、路由（manager）、消费（cli）各司其职，签名探测保证新旧 mock 共存。
下一步 step27: TurnEnd/GoalStatus 的真实产出（真实通道）+ from_config 事件桥接。
```

---

## 系统全景图（Step 26 全量）

```
                        ┌────────────────────────────────────────────────────┐
                        │              配置装配（step25 成果）                 │
                        │  load_config → AgentLoop.from_config(loop.py:180)  │
                        │  → make_provider → LLMRuntime 冻结(step22)         │
                        └────────────────────┬───────────────────────────────┘
                                             │
┌──────────┐   inbound   ┌──────────┐        ▼
│ 通道层     │───────────►│ MessageBus│  ┌──────────────────── 回合状态机 8 态 ────────────────────┐
│ channels/ │            │ (bus/    │  │ RESTORE→COMPACT→COMMAND→BUILD→RUN→SAVE→RESPOND→DONE     │
│ CliChannel│◄───────────│  queue)  │  │  RESTORE: session 恢复 + session_turn_started(409)      │
│  (REPL)   │   outbound │  events  │  │  COMMAND: CommandRouter(step21)                         │
└────┬─────┘            └────┬─────┘  │  RUN: 惰性装配 bus 回调(643-644/682-685)                  │
     │ send/send_delta       │        │    → run_status running(688) → record_runtime(691)      │
     │ typed 事件不结束 turn  │        │    → runner.run(spec)                                   │
     │ 最终消息才 set done    │        │       ├─ retry_wait_cb → RetryWaitEvent(图4)           │
     └── 状态行渲染 ──────────┤        │       ├─ progress_cb → ProgressEvent(图5)              │
                            │        │       ├─ provider.chat_stream_with_retry                │
                            │        │       │   └─ FallbackProvider 回退+熔断(step22)          │
                            │        │       ├─ 工具执行 → ToolRegistry → tools/*               │
                            │        │       │    ├─ echo/long_task/spawn                       │
                            │        │       │    └─ SpawnTool → SubagentManager(step23)        │
                            │        │       │         └─ announce → bus.inbound 回环注入        │
                            │        │       └─ hook.wants_streaming → ctx.on_stream(705)      │
                            │        │  SAVE: record_latency(724) → session/memory 持久化(step24)│
                            │        │  RESPOND: 最终消息(+StreamedResponseEvent, 758)          │
                            │        │  finally: turn_completed(339) → idle(345) → clear(348)  │
                            │        └──────────────────────────────┬────────────────────────────┘
                            │                                     │
  manager._dispatch_outbound(123)   typed 路由                     ▼
   ├─ StreamDeltaEvent → legacy send_delta                   RuntimeEventPublisher(135)
   ├─ StreamEndEvent → send_delta(stream_end)                 └─ RuntimeEventBus(78)
   ├─ Progress/RetryWait → 门控 send                              └─ 订阅者
   └─ 其余 → send + 退避重试                                            ├─ main.py:109 演示订阅者
                                                                       └─ 未来: WebUI / 状态机观测
```

---

*图中的英文词条为 step26 工程内英文常量/标识，行号以 step26 源码实测为准。*