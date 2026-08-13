# lean_nanobot — 待办清单（Backlog）

来源：**step32 vs nanobot 缺口分析**（对齐 `nanobot/agent/*` 与外圈 harness）。
每个条目给出：缺口、nanobot 参考实现、归属步骤（见 `ROADMAP.md`）、当前状态。
实现时按 AGENTS.md 规则：原理先行 → 单步独立 → 同名 .md 文档 → 最小增量 → 可运行验证 → 提交。

---

## A. Agent 核心（`nanobot/agent/*`）

| ID | 缺口 | 现状（step32） | nanobot 参考 | 归属步骤 | 状态 |
|----|------|---------------|--------------|---------|------|
| A1 | **Runtime 模型运行时解析层**：`ModelRuntimeResolver` + 不可变 `LLMRuntime` + model_presets + provider_signature 热刷新 + `resolve_override`；`replay_budget` 应从 context window 反推而非 main.py 手算 | 骨架已落地（`LLMRuntime` frozen + `ModelPreset` + loop 反推 budget）；完整 resolver/热刷新待后续 | `agent/model_runtime.py`、`agent/model_presets.py`、`loop.py:_replay_token_budget` | step22 / step25 / step33(_replay_token_budget) | ✅(骨架) ⬜(完整resolver) |
| A2 | **Mid-turn 注入打通**：`_state_run` 把 `injection_callback` 接到 pending_queue；子代理未结束时阻塞等待注入；`_MAX_INJECTIONS_PER_TURN` 参数、`_has_injection_content`、`allow_goal_continue` | 已全链接通（step23）：`_dispatch` 持锁注册 queue + finally re-publish；`_drain_pending` 阻塞等待；runner 注入过滤/上限/limit 参数/goal-continue 合并 | `loop.py:_drain_pending`、`runner.py:_drain_injections` | step23 | ✅ |
| A3 | **Subagent 系统消息通道**：`channel=="system"` 分支、`current_role="assistant"`、按 `subagent_task_id` 去重、前置持久化；隐藏历史标记 | system 通道已落地（step23）：`_process_system_message` + `_persist_subagent_followup`（assistant 带 `injected_event`/`subagent_task_id` 标记）；隐藏标记 step29 完成 | `loop.py:_process_system_message`、`_persist_subagent_followup` | step23 / step29 | ✅ |
| A4 | **_save_turn 持久化净化**：丢弃空 assistant；**校验并丢弃孤儿 tool result**（未在已声明 tool_call_id 内）；超 `max_tool_result_chars` 截断；多模态块净化；记录 latency | 已全链接通（step24）：`_save_turn` + `_sanitize_persisted_blocks`（简化版：text 截断 + 非 dict 保留；image 占位待媒体支持）、latency 打标；system 路径 skip 修正为 `len(initial_messages)` | `loop.py:_save_turn`、`_sanitize_persisted_blocks` | step24 | ✅(image 占位除外) |
| A5 | **Checkpoint 断点恢复**：进行中 assistant/tool 消息持久化到 metadata，崩溃后物化回历史（含 overlap 去重、pending tool call 补"interrupted"结果） | 已全链接通（step24）：`_set/_restore_runtime_checkpoint`（overlap 去重 + pending 补 interrupted）、runner `_emit_checkpoint` 三语义点（awaiting_tools/tools_completed/final_response）、`_state_restore`/`_process_system_message` 恢复点；/stop 物化 step29 完成 | `loop.py:_set/_restore_runtime_checkpoint`、`runner.py:_emit_checkpoint` | step24 / step29 | ✅ |
| A6 | **TurnContext 增强 + 状态机观测**：turn_id、runtime、request_context、runtime_context_blocks、progress/stream/retry_wait 回调、ephemeral、turn_scopes、StateTraceEntry 计时、turn_latency_ms | `turn_id`/`runtime`/`on_progress`/`on_stream`/`on_stream_end`/`pending_queue`/`request_context`/`runtime_context_blocks`/`ephemeral` 已补；turn_scopes/StateTraceEntry 未做 | `loop.py:TurnContext`、`StateTraceEntry` | step23 起步 | ✅(字段集) ⬜(StateTraceEntry) |
| A7 | **Hook 体系补齐**：`AgentTurnHookSpec`/`build_agent_turn_hook` 工厂、`AgentProgressHook`、`before/after_execute_tool`、`on_execute_tool_error`、`emit_reasoning`、`finalize_content`、tool_hint 输出 | step30 已完成 hook 工厂 + 生命周期钩子 + progress hook；`finalize_content` 在 max_iterations finalization 中尚未调用（step32 留待后续） | `agent/turn_hooks.py`、`agent/progress_hook.py`、`agent/hook.py` | step30 | ✅(主体) ⬜(finalize_content调用) |
| A8 | **Runner 健壮性收敛**：`should_execute_tools`（refusal/content_filter/error 禁工具）、max_iterations 后 finalization 重试、usage 估算回退、`tool_events` 字段、arrearage/配额识别、`fail_on_tool_error`、可定制 error_message/max_iterations_message、reasoning_content 提取 | step30 完成 should_execute_tools + usage 回退 + arrearage + error_message；step32 完成 max_iterations no-tool finalization + error/empty 注入排空 + governance 异常保护 + AgentRunResult.error/had_injections 字段；`tool_events`/`fail_on_tool_error` 未做 | `runner.py` 全量、`providers/base.py:should_execute_tools` | step30 / step32 | ✅(主体) ⬜(tool_events/fail_on_tool_error) |
| A9 | **运行时上下文提供器**：`RuntimeContextProvider` 每 turn 前动态解析上下文块并 append（时钟/状态等） | step28 已完成：`RuntimeContextProvider` 注册 + `_resolve_runtime_context_for_turn` + `append_runtime_context`；step31 完成展示期移除（`public_history_message`） | `runtime_context.py`、`loop.py:_resolve_runtime_context_for_turn` | step28 / step31 | ✅ |
| A10 | **Workspace 安全模型**：`WorkspaceScopeResolver`（restrict_to_workspace / sandbox / 项目路径 / access_mode）、ContextVar 绑定供工具查询、loopback 门禁 | step28 已完成：`WorkspaceScopeResolver` + ContextVar 绑定 + `read_file` 工具边界校验 + loopback 门禁 | `security/workspace_access.py` | step28 | ✅ |
| A11 | **Skills 体系**：SKILL.md frontmatter（requires: bins/env）、availability 过滤、workspace 覆盖、disabled_skills、渐进加载、参考注入 | step27 已完成：`SkillsLoader` + frontmatter + 可用性过滤 + disabled_skills + 渐进摘要 + always 全量注入 + ContextBuilder 注入 | `agent/skills.py`、`agent/context.py` | step27 | ✅ |
| A12 | **Turn continuation + 隐藏历史**：隐形续跑（跨 max_iterations 12 轮）、`_skip_user_persist`、`finalize_on_max_iterations`、`should_persist_user_message`；HIDDEN_HISTORY_META 过滤 | step29 完成隐形续跑 + 隐藏历史标记 + 调度并发；step31 完成公共历史接口（`get_public_history`）+ 运行时上下文展示期移除 | `session/turn_continuation.py`、`session/history_visibility.py` | step29 / step31 | ✅ |
| A13 | **调度与并发**：`_active_tasks` 跟踪、`_concurrency_gate`（全局并发信号量）、CancelledError 泄漏防护（`task_is_cancelling`） | step29 已完成：`_active_tasks` 跟踪 + `_cancel_active_tasks` + `utils/cancellation.py` | `loop.py:run/_dispatch`、`utils/cancellation.py` | step29 | ✅ |
| A14 | **FileStateStore / ExecSessionManager**：文件读/写去重追踪；shell 会话隔离 | 无 | `agent/tools/file_state.py`、`agent/tools/exec_session.py` | 未来候选 | ⬜ |
| A15 | **Session 历史回放增强**：`get_history` 增加 `extend_to_user`/`include_runtime_context` 参数、`recent_message_start_index` 切片、user turn 对齐、空 assistant 过滤、`_command` 过滤、字段白名单、`find_legal_message_start` 调用 | step32 的 `get_history` 仅简单尾部切片 + token 预算，无上述增强 | `session/manager.py:get_history`、`utils/helpers.py:recent_message_start_index` | step33 | ⬜(规划中) |
| A16 | **Consolidation Replay Overflow 压缩**：`_replay_overflow_boundary` + `_consolidate_replay_overflow` + `estimate_session_prompt_tokens` + `maybe_consolidate_by_tokens(replay_max_messages=)` | step32 的 consolidation 无 replay overflow 压缩，`maybe_consolidate_by_tokens` 无 `replay_max_messages` 参数；调用位置在 `_state_compact`（nanobot 在 `_state_build`） | `agent/memory.py:Consolidator`、`loop.py:_state_build` | step33 | ⬜(规划中) |
| A17 | **_run_agent_loop 提取**：核心循环提取为独立方法（217行），`_process_message` 只做准备和收尾 | step32 的核心循环逻辑在 `_process_message` 中直接调用状态机，未提取 | `loop.py:_run_agent_loop` | 未来候选(高风险重构) | ⬜ |
| A18 | **_persist_user_message_early 提前持久化**：turn 开始前持久化含运行时上下文 + marker 的用户消息到 session | step32 不持久化运行时上下文，marker 只在内存中的 initial_messages | `loop.py:_persist_user_message_early` | 未来候选 | ⬜ |
| A19 | **_drain_pending 阻塞等待 subagent**：subagent 仍在运行时阻塞等待 `pending_queue.get(timeout=300)`，保持 runner 循环存活 | step32 的 `_build_injection_callback` 无阻塞等待逻辑 | `loop.py:_run_agent_loop:_drain_pending` | 未来候选 | ⬜ |

---

## H. Harness 外圈（装配 / 配置 / 总线 / 通道 / 网关）

| ID | 缺口 | 现状（step32） | nanobot 参考 | 归属步骤 | 状态 |
|----|------|---------------|--------------|---------|------|
| H1 | **Config 配置系统**：schema + loader（`NANOBOT__` env、`${VAR}` 替换、迁移）；`agents.defaults`（fallback_models / session_ttl_minutes / consolidation_ratio / disabled_skills / tools.* / channels.* / gateway.*） | step25 已完成：schema + loader + 工厂双路分发 + `AgentLoop.from_config` + `Tool.config_cls()` 落地 | `config/schema.py`、`config/loader.py` | step25 | ✅ |
| H2 | **Providers Registry / Factory / Fallback / Snapshot**：ProviderSpec 自动探测、FallbackProvider 逐级回退、ProviderSnapshot（signature 热刷新） | step22 完成 registry(6 条目)+factory+异常式 FallbackProvider+Snapshot；自动探测/热刷新待后续 | `providers/registry.py`、`providers/factory.py`、`providers/fallback_provider.py` | step22 / step25 | ✅(异常式) |
| H3 | **装配 harness（from_config）**：`AgentLoop.from_config` 统一装配；outbound→session 镜像（`_deliver_to_channel`）；MessageTool 回调桥接；TokenUsageHook；健康检查 | step25 完成 `AgentLoop.from_config` 装配雏形；outbound 镜像/MessageTool 桥接/健康检查未做 | `cli/commands.py:_start_gateway_runtime` | step25 起步 | ✅(雏形) ⬜(完整) |
| H4 | **事件层**：typed outbound events（Progress / RetryWait / StreamEnd / StreamedResponse / TurnEnd / GoalStatus / SessionUpdated / RuntimeModelUpdated）+ 独立 RuntimeEventBus（turn started/completed、run status） | step26 已完成：outbound events 6 种 + RuntimeEventBus + progress callback 桥接 | `bus/outbound_events.py`、`bus/runtime_events.py`、`bus/progress.py` | step26 | ✅ |
| H5 | **Provider 重试引擎**：transient vs 不可重试（配额/欠费）分类、Retry-After 解析、`retry_mode=standard/persistent`、on_retry_wait 心跳、去图重试、角色交替强制（role alternation） | step30 完成 transient 分类 + Retry-After 解析 + retry_mode + on_retry_wait 心跳；去图重试/角色交替强制未做 | `providers/base.py`、`provider.py` | step30 | ✅(主体) ⬜(去图/角色交替) |
| H6 | **真实通道**：telegram / discord / slack / email / wecom / dingtalk / feishu 等；send_delta 带 stream_id/resuming、send_reasoning_delta、transcribe_audio、每通道 send_max_retries、entry_points 插件发现 | 仅 cli | `channels/*`、`channels/base.py` | 未来候选 | ⬜ |
| H7 | **安全**：SSRF 白名单、workspace sandbox、ingress 策略、loopback 门禁 | step28 完成 workspace sandbox + loopback 门禁；SSRF 白名单/ingress 策略未做 | `security/`、`webui/ingress_policy.py` | step28 | ✅(workspace) ⬜(SSRF/ingress) |
| H8 | **Session 键与可见性**：统一键 `unified:...` / `channel:chat_id`、history_visibility、automation 持久化 | step29 完成统一键 + history_visibility；automation 持久化未做 | `session/keys.py`、`session/history_visibility.py` | step29 | ✅ |
| H9 | **Cron / 触发器 / 自动化**：CronService（dream/heartbeat/bound）、LocalTriggerStore、automation_turns（隐藏历史标记） | 仅 `_dream_loop`（step15） | `cron/`、`triggers/`、`session/automation_turns.py` | 未来候选 | ⬜ |
| H10 | **日志**：loguru 统一日志替代散落 print | 大量 print（部分已改为 logging） | `utils/logging_bridge.py` | 任意步骤顺手 | ⬜ |
| H11 | **Gateway HTTP API / WebUI / SDK / OS 服务**：产品与部署层，本学习项目不追赶 | — | `api/`、`webui/`、`sdk/`、`gateway/service.py` | 不做（记录备查） | ⬜ |

---

## 路线衔接说明

- 每完成一项：更新状态 ✅，并在 `ROADMAP.md` 的对应 step 标注完成。
- 步骤内文件 import 规则：`step(N).` → `step(N+1).`，从上一 step fork。
- 所有测试禁止真实 API Key（AGENTS.md 原则 0）。
- 当前进度：**step32 已完成（297 tests），step33 规划中**（Session 历史回放增强 + Replay Overflow 压缩）。
