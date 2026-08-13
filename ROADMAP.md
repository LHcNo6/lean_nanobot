# lean_nanobot — 路线图

按最小增量对齐 nanobot 架构，复杂功能拆分为独立步骤。

---

## 已完成

| Step | 主题 | 核心文件 | 测试数 |
|------|------|----------|--------|
| 0–12 | 基础演进 | — | — |
| 13 | Context Governance | governance.py | 104 |
| 14 | Governance 增强 | governance.py + helpers.py | 104 |
| 15 | Consolidation + Dream + MemoryStore | memory.py + consolidation.py | 128 |
| 16 | Subagents + Sustained Goals | subagent.py + long_task.py + goal_state.py | 156 |
| 17a | Governance & Tool Execution Safety | runner.py + governance.py | 206 |
| 17b | Content Recovery & Continuation Control | runner.py + loop.py | 256 |
| 18 | ToolLoader & Tool System Upgrade | loader.py + tool.py + context.py | — |
| 19 | Session System Upgrade | session.py + autocompact.py | — |
| 20 | Channel Framework | channel.py + pairing.py + manager.py + channels/ | 318 |
| 21 | CommandRouter & COMMAND 状态 | command/router.py + builtin.py + loop.py | 341 |
| 22 | Providers Registry & Factory + Fallback | providers/registry.py + factory.py + fallback_provider.py + llm.py | 376 |
| 23 | Mid-turn Injection 打通 + Subagent 系统消息通道 | loop.py + runner.py + context.py + tools/spawn.py | 388 |
| 24 | Session 持久化净化 + Checkpoint 恢复 | loop.py + runner.py + tests/test_persistence.py | 411 |
| 25 | Pydantic 配置系统 | config/schema.py + config/loader.py + providers/factory.py | 451 |
| 26 | 事件层：typed outbound events + RuntimeEventBus | bus/outbound_events.py + runtime_events.py + progress.py | 388 + pytest 103 |
| 27 | Skills 加载器 | skills/loader.py + context.py | 388 + pytest 142 |
| 28 | Workspace 安全模型 + 运行时上下文 | security/workspace_access.py + runtime_context.py | pytest ~180 |
| 29 | Turn continuation + 隐藏历史 + 调度并发 | session/turn_continuation.py + history_visibility.py + loop.py | pytest ~220 |
| 30 | Reasoning + Hook 工厂 + Runner 健壮性收敛 | hook.py + runner.py + provider.py | 282 |
| 31 | 公共历史 + 运行时上下文展示期移除（A12 下半场） | runtime_context.py + session/manager.py + consolidation.py | 282 + 18 = 300 |
| 32 | Runner Finalization 对齐：max_iterations 无工具收尾 + error/empty 注入排空 + governance 异常保护 | runner.py + loop.py | 282 + 15 = 297 |

---

# 建议补齐路线（基于 step32 vs nanobot 缺口分析）

> 详细缺口清单见根目录 `todolist.md`（A1–A19 / H1–H11）。
> 路线以 **agent 核心正确性优先**（注入、持久化净化、历史回放、压缩），其次 harness 地基（config、事件层），
> 最后能力扩展（skills、安全、reasoning）；产品/部署层（HTTP API、WebUI、MCP）降为未来候选。

---

## Step 33 — Session 历史回放增强 + Replay Overflow 压缩（A15 + A16）🟡 规划中

**主题：** 对齐 nanobot 的 `get_history` 增强逻辑和 consolidation replay overflow 压缩机制

**优先级：** 高 — 用户明确要求"优先对齐 agent 循环部分和上下文、记忆、压缩部分"，本步覆盖记忆 + 压缩 + agent 循环调用位置调整。

| 改进 | 说明 |
|------|------|
| `helpers.py` | 新增 `recent_message_start_index(messages, max_messages, extend_to_user=)` — 尾部切片时可向前扩展到最近的 user turn |
| `session/manager.py:get_history` | 重写：增加 `extend_to_user`/`include_runtime_context` 参数；用 `recent_message_start_index` 替代简单尾部切片；避免从 turn 中间开始；`find_legal_message_start` 丢弃孤立 tool 结果；过滤 `_command` 消息；空 assistant 消息过滤；字段白名单（只保留 role/content/tool_calls/tool_call_id/name）；max_tokens 预算后 user turn 对齐 |
| `consolidation.py` | 新增 `_replay_overflow_boundary(session, replay_max_messages)` 静态方法；新增 `_consolidate_replay_overflow(session, replay_max_messages, runtime)`；新增 `estimate_session_prompt_tokens(session, runtime)`；`maybe_consolidate_by_tokens` 增加 `replay_max_messages` 参数 |
| `loop.py` | 新增 `_replay_token_budget(runtime)` 静态方法；`_state_compact` 去掉 consolidation 调用（只保留 auto_compact）；`_state_build` 中计算 `replay_max_messages` 并调用 `maybe_consolidate_by_tokens(replay_max_messages=)`；`get_history` 调用传 `extend_to_user=False` |

**不做什么（明确边界）：**
- 不做 `_run_agent_loop` 提取（纯重构，回归风险高）
- 不做 `_persist_user_message_early` 提前持久化（需要持久化策略变更）
- 不做 `_drain_pending` 阻塞等待 subagent（需要 subagent 运行状态跟踪）
- 不做 media / cli_apps breadcrumb（需要媒体处理基础设施）
- 不做 `_sanitize_assistant_replay_text`（留待后续研究）

**预期测试：** 297 + ~25 = ~322 passed

**导入：** 从 step32 fork，import `step32.` → `step33.`

---

## Step 34 — _persist_user_message_early 提前持久化 + _build_initial_messages 提取（A18）⬜ 待规划

**主题：** 运行时上下文持久化策略变更 + 初始消息构建提取

| 改进 | 说明 |
|------|------|
| `loop.py:_persist_user_message_early` | turn 开始前持久化含运行时上下文 + marker 的用户消息到 session（当前 marker 只在内存中） |
| `loop.py:_build_initial_messages` | 提取为独立方法，统一构建 initial_messages（当前内联在 `_state_build`） |
| `session/manager.py:get_history` | 回放前调用 `public_history_message` 避免运行时上下文重复追加 |

**风险：** 持久化策略变更可能影响摘要、token 估算、公共历史等多个下游，需充分回归。

---

## Step 35 — _run_agent_loop 提取为独立方法（A17）⬜ 待规划

**主题：** 纯重构 — 把核心循环从 `_process_message` 提取为 `_run_agent_loop`（217行）

| 改进 | 说明 |
|------|------|
| `loop.py:_run_agent_loop` | 独立方法，负责 checkpoint 回调、`_drain_pending` 注入回调、effective_scope/request_ctx 构建、hook 构建、`runner.run` 调用、结果处理 |
| `loop.py:_process_message` | 只做准备和收尾，调用 `_run_agent_loop` |

**风险：** 高 — 纯重构但涉及核心循环，需充分回归测试，建议在 step34 稳定后再做。

---

## Step 36 — _drain_pending 阻塞等待 subagent（A19）⬜ 待规划

**主题：** subagent 仍在运行时阻塞等待 pending_queue，保持 runner 循环存活

| 改进 | 说明 |
|------|------|
| `loop.py:_build_injection_callback` | 增加阻塞等待逻辑：无消息但 subagent 仍在运行时，`await asyncio.wait_for(pending_queue.get(), timeout=300)` |
| `subagent.py` | 增加 `get_running_count_by_session(session_key)` 方法 |

---

## 未来候选

| Step | 主题 | 说明 |
|------|------|------|
| 37+ | media / cli_apps breadcrumb | `get_history` 中 user 消息有 media 时合成 `[image: path]` 占位；有 cli_apps 时合成 CLI App Attachment 占位 |
| 37+ | `_sanitize_assistant_replay_text` | assistant 回放文本清理（去除内部标记、工具调用残留等） |
| 37+ | Hook `finalize_content` 调用 | 在 `_try_finalize_after_max_iterations` 成功后调用 `hook.finalize_content` |
| 37+ | `tool_events` / `fail_on_tool_error` | Runner 健壮性剩余部分 |
| 37+ | ModelRuntimeResolver 完整实现 | provider_signature 热刷新 + `resolve_override` |
| 37+ | Gateway & HTTP API | OpenAI 兼容 API（`POST /v1/chat/completions` SSE + `/v1/models` + `/health`）；产品层，待 agent 核心稳定后规划 |
| 37+ | MCP Integration | `mcp_<server>_<tool>` stdio/HTTP 客户端；需要 config 与工具注册稳定 |
| 37+ | 真实通道 | telegram / discord / slack / email / wecom / dingtalk / feishu（含 send_delta stream_id/resuming、send_reasoning_delta、transcribe_audio、每通道 send_max_retries、entry_points 插件发现） |
| 37+ | Cron / Triggers / 自动化 | CronService（dream/heartbeat/bound）、LocalTriggerStore、automation_turns（隐藏历史标记） |
| 37+ | FileStateStore / ExecSessionManager | 文件读/写去重追踪；shell 会话隔离 |
| 37+ | SSRF 白名单 / ingress 策略 | 安全剩余部分 |
| 37+ | Provider 去图重试 / 角色交替强制 | H5 剩余部分 |
| 37+ | StateTraceEntry 计时 | TurnContext 状态机观测剩余部分 |
| 37+ | loguru 统一日志 | 替代散落 print |
| — | WebUI / SDK / OS 服务 | 产品与部署层，本学习项目不追赶（记录备查） |
| — | memory.py 文件系统 / dream / legacy | 差距 849 行，但非核心循环，优先级低 |

---

## 设计原则

1. **最小增量** — 每步只改最少的文件，独立可测试
2. **向后兼容** — AgentRunSpec、AgentLoop 接口只加可选字段
3. **可拆分** — 复杂功能跨步骤，步间可通过 fork + import 变更串联
4. **测试先行** — 每步增加相应测试，不破坏原有测试
5. **原理先行** — 每步实现前先参考 nanobot，分析原理、选择方案、解释为什么、方案利弊，再决策
6. **核心优先** — agent 循环 / 上下文 / 记忆 / 压缩 优先于 harness 外圈和产品层
