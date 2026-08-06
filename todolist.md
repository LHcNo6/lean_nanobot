# lean_nanobot — 待办清单（Backlog）

来源：**step21 vs nanobot 缺口分析**（对齐 `nanobot/agent/*` 与外圈 harness）。
每个条目给出：缺口、nanobot 参考实现、归属步骤（见 `ROADMAP.md`）、当前状态。
实现时按 AGENTS.md 规则：原理先行 → 单步独立 → 同名 .md 文档 → 最小增量 → 可运行验证 → 提交。

---

## A. Agent 核心（`nanobot/agent/*`）

| ID | 缺口 | 现状（step21） | nanobot 参考 | 归属步骤 | 状态 |
|----|------|---------------|--------------|---------|------|
| A1 | **Runtime 模型运行时解析层**：`ModelRuntimeResolver` + 不可变 `LLMRuntime` + model_presets + provider_signature 热刷新 + `resolve_override`；`replay_budget` 应从 context window 反推而非 main.py 手算 | 骨架已落地（`LLMRuntime` frozen + `ModelPreset` + loop 反推 budget）；完整 resolver/热刷新待 step25 config | `agent/model_runtime.py`、`agent/model_presets.py`、`loop.py:_replay_token_budget` | step22 / step25 | ✅(骨架) |
| A2 | **Mid-turn 注入打通**：`_state_run` 把 `injection_callback` 接到 pending_queue（当前是死代码）；子代理未结束时阻塞等待注入；`_MAX_INJECTIONS_PER_TURN` 参数、`_has_injection_content`、`allow_goal_continue` | 已全链接通（step23）：`_dispatch` 持锁注册 queue + finally re-publish；`_drain_pending` 阻塞等待；runner 注入过滤/上限/limit 参数/goal-continue 合并 | `loop.py:_drain_pending`、`runner.py:_drain_injections` | step23 | ✅ |
| A3 | **Subagent 系统消息通道**：`channel=="system"` 分支、`current_role="assistant"`、按 `subagent_task_id` 去重、前置持久化；隐藏历史标记 | system 通道已落地（step23）：`_process_system_message` + `_persist_subagent_followup`（assistant 带 `injected_event`/`subagent_task_id` 标记）；隐藏标记留 step29 | `loop.py:_process_system_message`、`_persist_subagent_followup` | step23 / step29 | ✅(标记部分) |
| A4 | **_save_turn 持久化净化**：丢弃空 assistant；**校验并丢弃孤儿 tool result**（未在已声明 tool_call_id 内）；超 `max_tool_result_chars` 截断；多模态块净化；记录 latency | `import_messages(result.messages[skip:])` 原样入库，畸形消息会永久污染历史 | `loop.py:_save_turn`、`_sanitize_persisted_blocks` | step24 | ⬜ |
| A5 | **Checkpoint 断点恢复**：进行中 assistant/tool 消息持久化到 metadata，崩溃后物化回历史（含 overlap 去重、pending tool call 补"interrupted"结果） | 仅有简化 `_restore_pending_user_turn` | `loop.py:_set/_restore_runtime_checkpoint`、`runner.py:_emit_checkpoint` | step24 | ⬜ |
| A6 | **TurnContext 增强 + 状态机观测**：turn_id、runtime、request_context、runtime_context_blocks、progress/stream/retry_wait 回调、ephemeral、turn_scopes、StateTraceEntry 计时、turn_latency_ms | `turn_id`/`runtime`/`on_progress`/`on_stream`/`on_stream_end`/`pending_queue` 已补（step23）；request_context/runtime_context_blocks/ephemeral/turn_scopes/StateTraceEntry/turn_latency_ms 未做 | `loop.py:TurnContext`、`StateTraceEntry` | step23 起步 | ✅(字段集) |
| A7 | **Hook 体系补齐**：`AgentTurnHookSpec`/`build_agent_turn_hook` 工厂、`AgentProgressHook`、`before/after_execute_tool`、`on_execute_tool_error`、`emit_reasoning`、`finalize_content`、tool_hint 输出 | 静态 hook 列表 + 生命周期/流钩子子集 | `agent/turn_hooks.py`、`agent/progress_hook.py`、`agent/hook.py` | step30 | ⬜ |
| A8 | **Runner 健壮性收敛**：`should_execute_tools`（refusal/content_filter/error 禁工具）、max_iterations 后 finalization 重试、usage 估算回退、`tool_events` 字段、arrearage/配额识别、`fail_on_tool_error`、可定制 error_message/max_iterations_message、reasoning_content 提取 | 硬编码 fallback 文案；无估算回退；无 tool_events | `runner.py` 全量、`providers/base.py:should_execute_tools` | step30 | ⬜ |
| A9 | **运行时上下文提供器**：`RuntimeContextProvider` 每 turn 前动态解析上下文块并 append（时钟/状态等） | 无 | `runtime_context.py`、`loop.py:_resolve_runtime_context_for_turn` | step28 | ⬜ |
| A10 | **Workspace 安全模型**：`WorkspaceScopeResolver`（restrict_to_workspace / sandbox / 项目路径 / access_mode）、ContextVar 绑定供工具查询、loopback 门禁 | 工具只见 `workspace=""`、`config=None`（`loop.py:ToolContext`），无任何文件访问权限模型 | `security/workspace_access.py` | step28 | ⬜ |
| A11 | **Skills 体系**：SKILL.md frontmatter（requires: bins/env）、availability 过滤、workspace 覆盖、disabled_skills、渐进加载、参考注入 | ContextBuilder 只拼 `AGENTS.md/SOUL.md/USER.md` | `agent/skills.py`、`agent/context.py` | step27 | ⬜ |
| A12 | **Turn continuation + 隐藏历史**：隐形续跑（跨 max_iterations 12 轮）、`_skip_user_persist`、`finalize_on_max_iterations`、`should_persist_user_message`；HIDDEN_HISTORY_META 过滤 | 无；命令对不持久化属已知偏差（WebUI 前不补） | `session/turn_continuation.py`、`session/history_visibility.py` | step29 | ⬜ |
| A13 | **调度与并发**：`_active_tasks` 跟踪、`_concurrency_gate`（全局并发信号量）、CancelledError 泄漏防护（`task_is_cancelling`） | 无 | `loop.py:run/_dispatch`、`utils/cancellation.py` | step29 | ⬜ |
| A14 | **FileStateStore / ExecSessionManager**：文件读/写去重追踪；shell 会话隔离 | 无 | `agent/tools/file_state.py`、`agent/tools/exec_session.py` | 未来候选 | ⬜ |

---

## H. Harness 外圈（装配 / 配置 / 总线 / 通道 / 网关）

| ID | 缺口 | 现状（step21） | nanobot 参考 | 归属步骤 | 状态 |
|----|------|---------------|--------------|---------|------|
| H1 | **Config 配置系统**：schema + loader（`NANOBOT__` env、`${VAR}` 替换、迁移）；`agents.defaults`（fallback_models / session_ttl_minutes / consolidation_ratio / disabled_skills / tools.* / channels.* / gateway.*） | main.py 硬编码常量 | `config/schema.py`、`config/loader.py` | step25 | ⬜ |
| H2 | **Providers Registry / Factory / Fallback / Snapshot**：ProviderSpec 自动探测、FallbackProvider 逐级回退、ProviderSnapshot（signature 热刷新） | registry(6 条目)+factory+异常式 FallbackProvider+Snapshot 已落地；自动探测/热刷新待 step25 | `providers/registry.py`、`providers/factory.py`、`providers/fallback_provider.py` | step22 / step25 | ✅(异常式) |
| H3 | **装配 harness（from_config）**：`AgentLoop.from_config` 统一装配；outbound→session 镜像（`_deliver_to_channel`）；MessageTool 回调桥接；TokenUsageHook；健康检查 | main.py 内联硬编码装配 | `cli/commands.py:_start_gateway_runtime` | step26 起步 | ⬜ |
| H4 | **事件层**：typed outbound events（Progress / RetryWait / StreamEnd / StreamedResponse / TurnEnd / GoalStatus / SessionUpdated / RuntimeModelUpdated）+ 独立 RuntimeEventBus（turn started/completed、run status） | 2 队列 + 3 事件类型 | `bus/outbound_events.py`、`bus/runtime_events.py`、`bus/progress.py` | step26 | ⬜ |
| H5 | **Provider 重试引擎**：transient vs 不可重试（配额/欠费）分类、Retry-After 解析、`retry_mode=standard/persistent`、on_retry_wait 心跳、去图重试、角色交替强制（role alternation） | 仅少数 openai 异常分类 + 指数退避 | `providers/base.py`、`provider.py`（step21） | step30 | ⬜ |
| H6 | **真实通道**：telegram / discord / slack / email / wecom / dingtalk / feishu 等；send_delta 带 stream_id/resuming、send_reasoning_delta、transcribe_audio、每通道 send_max_retries、entry_points 插件发现 | 仅 cli | `channels/*`、`channels/base.py` | 未来候选 | ⬜ |
| H7 | **安全**：SSRF 白名单、workspace sandbox、ingress 策略、loopback 门禁 | 无 | `security/`、`webui/ingress_policy.py` | step28 | ⬜ |
| H8 | **Session 键与可见性**：统一键 `unified:...` / `channel:chat_id`、history_visibility、automation 持久化 | base64url 键 + atomic save 已对齐 | `session/keys.py`、`session/history_visibility.py` | step29 | ⬜ |
| H9 | **Cron / 触发器 / 自动化**：CronService（dream/heartbeat/bound）、LocalTriggerStore、automation_turns（隐藏历史标记） | 仅 `_dream_loop` | `cron/`、`triggers/`、`session/automation_turns.py` | 未来候选 | ⬜ |
| H10 | **日志**：loguru 统一日志替代散落 print | 大量 print | `utils/logging_bridge.py` | 任意步骤顺手 | ⬜ |
| H11 | **Gateway HTTP API / WebUI / SDK / OS 服务**：产品与部署层，本学习项目不追赶 | — | `api/`、`webui/`、`sdk/`、`gateway/service.py` | 不做（记录备查） | ⬜ |

---

## 路线衔接说明

- 每完成一项：更新状态 ✅，并在 `ROADMAP.md` 的对应 step 标注完成。
- 步骤内文件 import 规则：`step(N).` → `step(N+1).`，从上一 step fork。
- 所有测试禁止真实 API Key（AGENTS.md 原则 0）。
