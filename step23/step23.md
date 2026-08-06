# Step 23 — Mid-turn Injection 打通 + Subagent 系统消息通道

在 Step 22 (Providers Registry & Factory + Fallback) 基础上，对齐 nanobot 的
`agent/loop.py` + `agent/runner.py` 注入语义：让 subagent 回包（系统消息通道）
在**当前 turn 内**被注入处理，而不是排队成独立 turn（A2 + A3 + A6）。

---

## 一、这一阶段解决了什么问题、为什么要这样做

Step 21 引入 `runner.py` 的 `spec.injection_callback` 时，`loop.py` 从没传过
这个回调——**注入是死代码**。subagent 完成后的回包（`channel=="system"`）走
普通消息路径 `_process_message`，与用户消息竞争 session 锁：

- **并发错乱**：子代理回包到达时若 session 锁被占用，会排队成独立 turn，
  与主 turn 交错；若锁空闲则直接抢跑成新 turn，打乱对话顺序；
- **重复注入**：同一次子代理结果没有去重标记，多轮重复注入；
- **历史无法对齐**：注入的消息没有持久化标记，`import_messages` 后无法从
  历史中识别「这是子代理结果」；
- **角色交替破坏**：subagent 回包持久化后，历史末尾是 assistant 消息，
  再拼 user 消息会连续同角色，被部分 provider 拒绝。

nanobot 的做法（`loop.py:_drain_pending` + `runner.py:_drain_injections` +
`_process_system_message`）把系统消息接到 pending queue，在 turn 内注入；
本 step 对齐这条链。

## 二、目标与实现

| 目标 | 实现 |
|------|------|
| 注入回调全链接通 | `loop.py:_state_run` / `_process_system_message` 通过 `_build_injection_callback` 把 pending_queue 接到 runner 的 `injection_callback`；`_drain_pending` 空队列且子代理仍运行时**阻塞等待**（300s 超时） |
| `_dispatch` 重写 | 按 nanobot 语义：持锁注册 pending queue，finally 中 identity 判空后弹栈；剩余消息 re-publish 到 `bus.inbound`（不静默丢失）；删除旧 `_drain_leftover` |
| runner 注入升级 | `_has_injection_content`（过滤 None/空白/空列表）、`_drain_injections` 支持 `limit` 参数（签名探测向后兼容）、`_MAX_INJECTIONS_PER_TURN=3`、`_MAX_INJECTION_CYCLES=5`、`allow_goal_continue`；goal-continue 并入统一的 `_try_drain_injections`（工具执行后 + final 后各一次） |
| system 消息通道 | `channel=="system"` 分支 `_process_system_message`：subagent 回包 `current_role="assistant"`、按 `subagent_task_id` 去重、**前置持久化**（先落库再拼 prompt），随后正常跑 runner 并在本 turn 回复 |
| 角色交替 | `context.py:build_messages(current_role=...)`：历史末尾与 current_role 相同则 merge 内容（拷贝 dict，不污染 session 历史） |
| TurnContext 增强 | 补 `turn_id`（`session_key:time_ns`）/ `runtime` / `on_progress` / `on_stream` / `on_stream_end` / `pending_queue` 字段 |
| spawn 工具透传 session_key | `SpawnTool.execute` 从 `current_request_context()` 取 session_key 传给 `SubagentManager.spawn`，保证 announce 路由回正确 session（`_session_tasks` 跟踪 / 注入等待判定依赖它） |

## 三、核心函数 / 类说明

### `runner.py`
- `_has_injection_content(payload)`：空值过滤器（None / 空白字符串 / 空列表 → False）。
- `_drain_injections(messages, limit, allow_goal_continue, ...)`：调用
  `spec.injection_callback`，用 `inspect.signature` 探测是否接受 `limit` kwarg
  （兼容旧回调）；结果过 `_has_injection_content` 后包装成 user 消息追加。
- `_try_drain_injections(...)`：注入上限（cycles）、goal-continue 合并；
  工具执行后与 final response 后统一入口。LLM error 路径**不回退**注入
  （见取舍）。
- `_MAX_INJECTIONS_PER_TURN=3` / `_MAX_INJECTION_CYCLES=5`：注入上限常量。

### `loop.py`
- `_dispatch`：`session_key` 归一 → 每 session 一把锁；锁占用则入
  pending queue（未来注入）；否则持锁注册 queue、跑 `_process_message`、
  finally 中 identity 判空弹栈 + 剩余 re-publish。
- `_process_message(msg, session_key, *, pending_queue, runtime)`：
  `channel=="system"` 走 `_process_system_message`，其余走状态机。
- `_process_system_message`：subagent 回包专属路径——`_persist_subagent_followup`
  去重持久化（assistant 消息带 `injected_event="subagent_result"` +
  `subagent_task_id`）→ `current_role="assistant"`、`current_message=""`
  → 跑 runner（含 injection_callback）→ `import_messages(result.messages[skip:])`。
- `_build_injection_callback(pending_queue, session_key, session)`：返回
  `_drain_pending`——先 `get_nowait` 排干队列；空队列且
  `get_running_count_by_session(session_key) > 0` 时 `wait_for(queue.get(), 300)`
  阻塞等待子代理 announce（对齐 nanobot 保持 turn 存活）。
- `_persist_subagent_followup(session, msg)`：同一 `subagent_task_id` 已持久化
  则跳过（去重），否则追加 assistant 消息并带注入标记。
- `_build_agent_spec`：抽出 spec 装配（ToolContext 带 `session_key`、
  `goal_continue_message`、`injection_callback`、goal_active_predicate）。
- `TurnContext`：新增 `turn_id / runtime / on_progress / on_stream /
  on_stream_end / pending_queue`。

### `context.py`
- `build_messages(..., current_role="user")`：消息尾部角色与 current_role
  相同则把 current_message merge 进最后一条（**先拷贝 dict**，避免污染
  session.messages 引用）；subagent 前置持久化后 `current_message=""` 时
  直接返回，不产生空 assistant 占位。
- `ToolContext`：新增 `session_key` 字段。

### `tools/spawn.py`
- `SpawnTool.execute`：`current_request_context().session_key` → `manager.spawn(..., session_key=...)`。

## 四、暴露的问题 / 偏离与取舍

| 取舍 | 说明 | 后续计划 |
|------|------|----------|
| LLM error 不回退注入 | nanobot 在 error 路径也会 drain 注入；我们保留 step21 测试契约（`test_error_with_injection_callback` 要求 error 时注入 0 次） | step30 错误语义收敛时再对齐 |
| 只持久化标记，不隐藏 | 注入消息带 `injected_event`/`subagent_task_id` 标记持久化，但不做 HIDDEN_HISTORY_META 过滤 | step29（A12）隐藏历史 |
| 去重范围 | 按 `subagent_task_id` 仅在**同一 session** 内去重；跨 session 幂等依赖 pending 队列单次消费 | 无需额外处理 |
| 阻塞等待 300s | 子代理未结束时 `_drain_pending` 阻塞；若子代理异常不再 announce，会拖满 300s 后继续（注入为空、不报错） | step24 checkpoint 后评估超时策略 |
| 双路径并存 | 锁空闲时 system 消息走独立 turn（`_process_system_message`）；锁占用时走 pending 注入（只有标记、不前置持久化）——两条路径共用同一持久化标记 | — |
| e2e 测试时序 | 子代理 e2e 用 0.1s 延迟子代理 + 轮询 watcher 观测运行窗口，避免瞬时完成导致断言错过 | 测试用 mock，无真实 API |

## 五、下一步要解决什么

Step 24 — Session 持久化净化 + Checkpoint 恢复（A4 + A5）：`_save_turn`
丢弃空 assistant / 孤儿 tool result / 超长截断，防畸形消息污染历史；
`_set/_restore_runtime_checkpoint` 让崩溃后 turn 可恢复。
