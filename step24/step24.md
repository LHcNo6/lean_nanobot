# Step 24 — Session 持久化净化 + Checkpoint 恢复

在 Step 23 (Mid-turn Injection 打通 + Subagent 系统消息通道) 基础上，对齐 nanobot 的
`agent/loop.py` 的 `_save_turn` / `_sanitize_persisted_blocks` 与
`_set/_restore_runtime_checkpoint`（A4 + A5）。

---

## 一、这一阶段解决了什么问题、为什么要这样做

Step 23 及之前的 `_state_save` / `_process_system_message` 用
`session.import_messages(result.messages[skip:])` **原样入库**，存在两类问题：

1. **畸形消息永久污染历史（A4）**：
   - 空 assistant 消息（`content=""` 且无 `tool_calls`）写进 JSONL 后每次
     replay 都会进 prompt，挤占上下文；
   - **孤儿 tool result**（`tool_call_id` 从未被任何 assistant 的
     `tool_calls` 声明过）会破坏 provider 的 tool_call/tool 配对，
     部分 provider 直接报错；`governance.py:drop_orphan_tool_results`
     只在**发请求前**净化内存消息，救不了已落库的历史；
   - 超长 tool result（数万字符）撑爆 replay budget；
   - 多模态 list 内容未经净化直接落盘（为 step28 媒体支持预留边界）。

2. **崩溃丢进行中上下文（A5）**：只有 `_restore_pending_user_turn`
   （user 消息已存但无回复时补一句 error 占位）。如果进程在**工具执行中途**
   崩溃——assistant 已发出 `tool_calls`、部分 tool result 已返回——这些
   进行中消息整体丢失，恢复后 user 消息直接接 user 消息，角色交替被破坏。

nanobot 的做法：`_save_turn` 在**落库前**净化（空 assistant 丢弃、孤儿
tool result 校验、超长截断、list 块净化、latency 打标）；`_emit_checkpoint`
把进行中 turn 的快照（assistant + 已完成 tool 结果 + 待执行 tool call）
同步写进 session metadata，崩溃后 `_restore_runtime_checkpoint` 物化回
历史（含 overlap 去重、pending call 补 "interrupted" 结果）。本 step 对齐
这条链。

## 二、目标与实现

| 目标 | 实现 |
|------|------|
| 落库净化 | `loop.py:_save_turn(session, messages, skip, *, turn_latency_ms)` 替代 `import_messages`：空 assistant 跳过；孤儿 tool result 丢弃（warning）；str 超 `max_tool_result_chars`（默认 16000）截断；list 内容过 `_sanitize_persisted_blocks`，空则补 `[tool result omitted during persistence]` 占位（保 assistant/tool 配对） |
| 多模态净化 | `_sanitize_persisted_blocks(content, *, should_truncate_text)`：非 dict 块保留、text 块按需截断、其余 dict 原样（简化版，image_url 占位等媒体支持再加） |
| declared id 追踪 | `_save_turn` 从 session 历史收集已声明 id，turn 内逐条动态追加（同一 turn 内 assistant 声明 + 后续 tool result 也合法） |
| latency | `TurnContext.turn_wall_started_at`（`_process_message` 起点记时）→ `_state_save`/`_process_system_message` 计算 ms 传入 `_save_turn`，打在最后 assistant 消息的 `latency_ms` |
| Checkpoint 写入 | `_set_runtime_checkpoint(session, payload)`：metadata 存快照 + **同步落盘**；`_build_checkpoint_callback` 装配到 runner 的 `spec.checkpoint_callback` |
| Checkpoint 发射 | `runner.py:_emit_checkpoint` 3 个语义点：`awaiting_tools`（assistant 带 tool_calls，pending=全部）、`tools_completed`（结果全返回）、`final_response`（正常定稿 + 注入续跑路径，`_try_drain_injections` 内） |
| Checkpoint 恢复 | `_restore_runtime_checkpoint`：重组 assistant + completed + pending（补 `Error: Task interrupted before this tool finished.`）；**overlap 去重**（`_checkpoint_message_key` 元组比对最长公共后缀）；清 pending_user_turn + checkpoint |
| 恢复时机 | `_state_restore` 与 `_process_system_message` 开头，先 checkpoint 后 pending_user_turn |
| 清理 | `_state_save` / `_process_system_message` 落库后 `_clear_runtime_checkpoint` |
| system 路径 skip 修正 | `_process_system_message` 的 `skip` 从 `2 + len(history)` 改为 `len(initial_messages)`：subagent 路径（current_role="assistant" 且 current_message="" 时不追加新消息）initial 只有 `1 + len(history)`，旧公式会**丢掉最后一轮回复** |

## 三、核心函数 / 类说明

### `loop.py`
- `_save_turn(session, messages, skip, *, turn_latency_ms)`：逐条净化
  `messages[skip:]` 后追加到 session；assistant 消息动态扩展 declared id；
  末尾 assistant 打 `latency_ms`；更新 `updated_at`。
- `_sanitize_persisted_blocks(content, *, should_truncate_text)`：list 块
  过滤器（简化版）。
- `_build_checkpoint_callback(session)`：返回 `_checkpoint(payload)` 闭包，
  session 为 None 时返回 None（dream 等无 session 场景不写 checkpoint）。
- `_set_runtime_checkpoint` / `_clear_runtime_checkpoint` / `_checkpoint_message_key`
  / `_restore_runtime_checkpoint`：快照写读清 + 元组键 overlap 去重。
- `TurnContext`：新增 `turn_wall_started_at`。
- `AgentLoop`：新增 `max_tool_result_chars=16_000` 构造参数、
  `_RUNTIME_CHECKPOINT_KEY = "runtime_checkpoint"`。

### `runner.py`
- `AgentRunSpec.checkpoint_callback: Callable[[dict], Awaitable[None]] | None`。
- `_emit_checkpoint(spec, payload)`：非空回调则 await。
- `_openai_tool_calls(tool_calls)`：ToolCallRequest → openai tool_call dict
  （payload 的 pending_tool_calls 用）。
- `_run_loop`：`awaiting_tools` / `tools_completed` 发射点（工具轮内）；
  `final_response` 发射点（定稿后）。`_try_drain_injections` 在续跑时
  发射 `final_response`（assistant_message + iteration 都非空）。

## 四、暴露的问题 / 偏离与取舍

| 取舍 | 说明 | 后续计划 |
|------|------|----------|
| 多模态净化简化版 | 只做 text 截断 + 非 dict 保留，未做 image_url data: 占位（step23 无媒体输入） | step28 媒体支持时补 `image_placeholder_text` |
| 快照为"最新即覆盖" | checkpoint payload 是当前进度快照，后发覆盖先发（与 nanobot 一致），不是增量日志 | — |
| `/stop` 未物化 | nanobot 在 CancelledError 里先 `_restore_runtime_checkpoint` 再 raise；step24 只做恢复点接入，/stop 物化留 step29 并发收敛 | step29（A13） |
| 发射频率 | 每工具轮 2 次落盘（awaiting + completed）+ final 1 次，高频会话落盘开销增加 | 可后续评估节流 |
| system skip 修正连带 | `2 + len(history)` → `len(initial_messages)` 改变了旧行为（旧实现会丢 subagent 轮末回复），本 step 一并修正并有测试覆盖 | — |
| `_save_turn` 与 governance 的关系 | 二者净化逻辑独立：governance 管"发请求前"（内存），`_save_turn` 管"落库后"（磁盘），互不替代 | — |

## 五、下一步要解决什么

Step 25 — Pydantic 配置系统（H1）：`config/schema.py` + `config/loader.py`，
把 `max_tool_result_chars`、replay_budget 等散落常量收敛进配置，消除
main.py 硬编码。
