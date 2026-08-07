# Step 26 — 事件层：typed outbound events + RuntimeEventBus

在 Step 25 (Pydantic 配置系统) 基础上，把顶层 `bus.py` / `events.py` 收纳为 `bus/` 包
（对齐 nanobot `nanobot/bus/` 布局），并补齐事件层：typed outbound events
（挂在 `OutboundMessage.event` 上）+ 进程内 RuntimeEventBus（H4 子集）+ provider
重试心跳，loop / manager / cli 通道全部接上，为真实通道与状态机观测铺路。

---

## 一、这一阶段解决了什么问题、为什么要这样做

Step 25 之前，outbound 只有裸消息 + `metadata` 魔法 flag：运行时/UI 语义
（进度、重试等待、流式结束、最终响应等）散落在各调用点手工拼 flag，通道侧靠
猜字符串判断，类型与路由都不可靠。

nanobot 的做法（`nanobot/bus/`）：
- `events.py`：消息总线仍然运输 `OutboundMessage`（通道需要 chat 路由字段），
  但把运行时/UI 语义挂在消息的 `event` 字段上，事件是 **typed dataclass**；
- `outbound_events.py`：Progress / RetryWait / StreamEnd / StreamedResponse /
  TurnEnd / GoalStatus 等事件类型 + `outbound_message_for_event` 工厂；
- `runtime_events.py`：**独立于消息总线**的进程内 pub/sub（SessionTurnStarted /
  TurnRunStatusChanged / TurnCompleted），订阅者可选（WebUI 适配器、CLI 演示等）；
  消息总线负责用户/聊天交付，运行时事件负责进程内状态通知，两者不混；
- `progress.py`：把 agent 进度回调转成 ProgressEvent 发布到消息总线；
- provider 长退避重试期间周期上报 `on_retry_wait` 心跳，让 UI 不至于"无响应"。

本 step 对齐这条链路的最小集（roadmap H4）。

## 二、目标与实现

| 目标 | 实现 |
|------|------|
| 包重排 | 顶层 `bus.py` / `events.py` → `bus/` 包（`queue.py` / `events.py` / `outbound_events.py` / `runtime_events.py` / `progress.py`），`bus/__init__.py` 重导出保证 `from step26.bus import MessageBus` 旧引用不变 |
| typed outbound events | `bus/outbound_events.py`：6 个 frozen dataclass（`ProgressEvent` / `RetryWaitEvent` / `StreamEndEvent` / `StreamedResponseEvent` / `TurnEndEvent` / `GoalStatusEvent`，后两个本 step 只定义类型）+ `outbound_message_for_event`（content 缺省时从事件推导）+ `_event_content` |
| RuntimeEventBus | `bus/runtime_events.py`：`RuntimeEventBus.subscribe(handler, event_type=...)` 返回退订函数；`publish` 按注册顺序派发并 await 异步 handler；handler 异常隔离（吞掉记日志，不影响其它订阅者）；`publish_nowait` 供同步调用点（无运行中事件循环则丢弃） |
| RuntimeEventPublisher | `record_turn_runtime` / `record_turn_latency` / `clear_turn` 暂存 per-turn 数据；`session_turn_started` / `run_status_changed` / `turn_completed` 三种派发（turn 结束时弹出 latency/runtime 一并携带）；`ensure_runtime_event_publisher(owner)` 惰性装配 |
| progress 回调 | `bus/progress.py:build_bus_progress_callback(bus, msg)` 返回签名对齐 nanobot 的回调（content 必填 + tool_hint/tool_events/file_edit_events/reasoning/reasoning_end 关键字），发布 ProgressEvent 到 outbound |
| provider 重试心跳 | `provider.py:chat_with_retry` / `chat_stream_with_retry`：长退避分段上报 `on_retry_wait("...attempt N, retry in Xs")`；`FallbackProvider` 与 runner 原样透传 |
| loop 集成 | `session_turn_started`（turn 开始时）；`run_status_changed(running, started_at)`（runner 前）+ `record_turn_runtime`；`_state_save` 记录 latency；`finally` 中 `turn_completed` + `run_status_changed(idle)` + `clear_turn`；`_state_respond` 在流式交付时给最终消息挂 `StreamedResponseEvent`；工具进度/重试等待经 `_build_bus_progress_callback` / `_build_retry_wait_callback` 装配 |
| manager 路由 | `_dispatch_outbound`：`StreamEndEvent` → `send_delta(stream_end=True, stream_id=..., resuming=...)`；`Progress/RetryWait` 有内容才 `send`；`StreamedResponseEvent` 等其余走普通 `send`（含重试）；legacy `StreamDeltaEvent` 保留兼容 |
| cli 通道 | `CliChannel.send`：Progress/RetryWait 只作状态行打印（有内容时），**不结束 turn**；只有最终消息才 `_turn_done.set()`，保住"等待最终响应"语义 |
| 测试 | `tests/test_events.py`（pytest，40 个，全构造数据 / mock provider，无真实 API Key）：事件字段/默认值/frozen；工厂 content 推导与 metadata 路由；RuntimeEventBus 订阅/类型过滤/异步 handler 顺序/异常隔离/退订；publisher 三种派发与暂存弹出；progress 回调；provider 重试心跳（chat/chat_stream）；loop 全 turn 生命周期事件、最终消息挂 StreamedResponseEvent、工具进度与重试等待出站；manager StreamEnd/Progress 路由；cli turn 语义 |

## 三、核心函数 / 类说明

### `bus/outbound_events.py`
- `OutboundEvent`：marker 基类（isinstance 判定事件路由）。
- 6 个 frozen dataclass：字段对齐 nanobot；`ProgressEvent` 的
  `tool_hint / tool_events / file_edit_events / reasoning*` 在 step30 hook
  体系补齐后才置位；`TurnEndEvent / GoalStatusEvent` 本 step 仅定义类型。
- `outbound_message_for_event(*, channel, chat_id, event, content=None, metadata=None)`：
  构造带 typed event 的 OutboundMessage；content 缺省时走 `_event_content`
  （Progress/RetryWait/StreamEnd 取自身 content，其余为 ""）。

### `bus/runtime_events.py`
- `RuntimeEventBus`：`subscribe` 返回退订闭包；`publish` 串行派发（事件严格
  跟随用户消息排布）；handler 抛异常不影响其它订阅者；`publish_nowait` 用
  `loop.create_task` 后台派发。
- `RuntimeEventPublisher`：per-turn 暂存 latency/runtime，`turn_completed`
  弹出并随事件派发；`clear_turn` 在 run status 复位 idle 时调用。
- `ensure_runtime_event_publisher(owner)`：优先复用 `owner.runtime_event_publisher`，
  否则在 `owner.runtime_events` 上建 bus 并包装（对齐 nanobot 同名函数）。

### `bus/progress.py`
- `build_bus_progress_callback(bus, msg)`：闭包持有 channel/chat_id/metadata，
  每次调用发布一条 `ProgressEvent` 出站；签名与 nanobot 一致，runner 按
  `inspect.signature` 探测后调用，旧式回调（只收 content）可复用。

### `loop.py`
- `_runtime_events()`：惰性取 `self.runtime_event_publisher`。
- `_build_bus_progress_callback` / `_build_retry_wait_callback`：把 bus 与
  msg 包成回调注入 runner；重试等待回调发布 `RetryWaitEvent`。
- turn 生命周期：`session_turn_started` → `run_status_changed(running)` →
  `record_turn_runtime/latency` → `turn_completed` + `run_status_changed(idle)`
  + `clear_turn`（finally 保证成败都派发）。
- `_state_respond`：`hook.wants_streaming()` 判定流式交付，最终消息挂
  `StreamedResponseEvent`（error/tool_error 除外）。

### `manager.py`
- `_dispatch_outbound`：按 `msg.event` 类型分流：`StreamEndEvent` 走
  `send_delta(stream_end)`；`Progress/RetryWait` 有内容才 `send`；其余
  （含 StreamedResponseEvent 最终消息）走带退避重试的 `send`。

### `channels/cli.py`
- `CliChannel.send`：typed 运行时事件只作状态行打印、不 set `_turn_done`；
  最终消息打印后 `_turn_done.set()`；`send_delta` 按 `(chat_id, stream_id)`
  缓冲、`stream_end` 时拼全量输出。

### `provider.py`
- `chat_with_retry / chat_stream_with_retry`：`on_retry_wait` 心跳在每次
  退避 sleep 前回调（文本含 attempt 序号与等待秒数），长等待也能让 UI 感知。

## 四、暴露的问题 / 取舍

| 取舍 | 说明 | 后续计划 |
|------|------|----------|
| TurnEnd/GoalStatus 只定义类型 | 本 step 无人产出：真实通道在收到最终响应后才生成（对齐 nanobot websocket/webui_turns） | 真实通道步 |
| GoalStatusEvent.status 必填 | 对齐 nanobot 目标状态语义，`started_at` 可选 | — |
| publish_nowait 依赖事件循环 | 无运行中 loop 时丢弃并记 debug 日志（同步调用点的必然限制） | — |
| 工具进度低噪声 | 工具执行完成只发一条 ProgressEvent（tool_hint=False）；推理增量/文件编辑等字段留空 | step30 hook 体系 |
| 流式判定 | 用 `hook.wants_streaming()` 近似 nanobot 的 `on_stream` 判定；最终消息挂 StreamedResponseEvent 而非逐段事件 | 真实流式通道步 |
| 双事件总线并存 | 消息总线（用户/聊天交付）与运行时事件总线（进程内状态通知）分离，订阅者可选；CLI 演示订阅者见 `main.py` | WebUI 适配器 |

## 五、下一步要解决什么

Step 27 — 真实通道与事件消费：把 `TurnEndEvent / GoalStatusEvent` 的产出落回
真实通道（收到最终消息后生成），`AgentLoop.from_config` 向 `channels.__init__`
的事件回调桥接演进；`GoalStateChanged / RuntimeModelChanged` 等运行时事件
继续对齐 nanobot；并把 `StreamedResponseEvent` 语义在 CLI/流式通道上完整呈现。
