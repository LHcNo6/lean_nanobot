# lean_nanobot — 待办清单（Backlog）

来源：**step40 vs nanobot 缺口分析**（详见 `align.md`，对齐 `nanobot/agent/*` 与外圈 harness）。
每个条目给出：缺口、nanobot 参考实现、归属步骤（见 `roadmap.md`）、当前状态。
实现时按 AGENTS.md 规则：原理先行 → 单步独立 → 同名 .md 文档 → 最小增量 → 可运行验证 → 提交。

---

## A. Agent 核心（`nanobot/agent/*`）— 优先对齐

| ID | 缺口 | 现状（step40） | nanobot 参考 | 归属步骤 | 状态 |
|----|------|---------------|--------------|---------|------|
| A1 | **Runtime 模型运行时解析层**：`ModelRuntimeResolver` + 不可变 `LLMRuntime` + model_presets + provider_signature 热刷新 + `resolve_override` | 骨架已落地（`LLMRuntime` frozen + `ModelPreset` + loop 反推 budget）；完整 resolver/热刷新待后续 | `agent/model_runtime.py`、`agent/model_presets.py` | step22/25/33 ✅骨架 / step55 完整resolver | ✅(骨架) ⬜(完整resolver) |
| A2 | **Mid-turn 注入打通**：`injection_callback` 接 pending_queue；子代理未结束时阻塞等待；`_MAX_INJECTIONS_PER_TURN`、`_has_injection_content`、`allow_goal_continue` | 已全链接通（step23）；step40 `_run_agent_loop` 提取后保留 | `loop.py:_run_agent_loop`、`runner.py:_drain_injections` | step23 | ✅ |
| A3 | **Subagent 系统消息通道**：`channel=="system"` 分支、`current_role="assistant"`、按 `subagent_task_id` 去重、前置持久化；隐藏历史标记 | system 通道已落地（step23）；隐藏标记 step29 完成 | `loop.py:_process_system_message`、`_persist_subagent_followup` | step23/29 | ✅ |
| A4 | **_save_turn 持久化净化**：丢弃空 assistant；校验并丢弃孤儿 tool result；超 `max_tool_result_chars` 截断；多模态块净化；记录 latency | step24 完成主体；step40 已写入 `latency_ms`；image_url data: 占位、`_meta`/`RUNTIME_CONTEXT_MESSAGE_META` 处理、`updated_at` 类型统一未做 | `loop.py:_save_turn`、`_sanitize_persisted_blocks` | step24/40 ✅(主体+latency) / step45(image/meta/type) | ✅(主体) ⬜(image/meta/type) |
| A5 | **Checkpoint 断点恢复**：进行中 assistant/tool 消息持久化到 metadata，崩溃后物化回历史（含 overlap 去重、pending tool call 补"interrupted"结果） | 已全链接通（step24/29） | `loop.py:_set/_restore_runtime_checkpoint`、`runner.py:_emit_checkpoint` | step24/29 | ✅ |
| A6 | **TurnContext 增强 + 状态机观测**：turn_id、runtime、request_context、runtime_context_blocks、progress/stream/retry_wait 回调、ephemeral、turn_scopes、StateTraceEntry 计时、turn_latency_ms | 字段集已补（turn_id/runtime/回调/request_context/runtime_context_blocks）；step40 `_run_agent_loop` 已支持 turn_scopes/hook_factories 参数（但 TurnContext 本身无字段）；StateTraceEntry 未做；latency_ms 已在 _save_turn 写入 | `loop.py:TurnContext`、`StateTraceEntry` | step23 起步 / step40(turn_scopes参数) / step44(StateTraceEntry) / step57(TurnContext字段重构) | ✅(字段集+turn_scopes参数) ⬜(TurnContext.turn_scopes字段/StateTraceEntry) |
| A7 | **Hook 体系补齐**：`AgentTurnHookSpec`/`build_agent_turn_hook` 工厂、`AgentProgressHook`、`before/after_execute_tool`、`on_execute_tool_error`、`emit_reasoning`、`finalize_content`、tool_hint 输出 | step30 完成 hook 工厂 + 生命周期钩子 + progress hook；step40 完成 hook_factories 两层分离；`finalize_content` 调用 + `emit_reasoning` + runner 侧 before/after_execute_tool 未做 | `agent/turn_hooks.py`、`agent/hook.py` | step30/40 ✅(工厂+factories) / step48(finalize_content+reasoning) / step50(runner hook生命周期) | ✅(工厂+factories) ⬜(finalize_content调用/emit_reasoning/runner侧hook) |
| A8 | **Runner 健壮性收敛**：`should_execute_tools`、max_iterations finalization、usage 估算回退、`tool_events`、arrearage 识别、`fail_on_tool_error`、可定制 error_message/max_iterations_message、reasoning 提取 | step30/32 完成 should_execute_tools + usage 回退 + arrearage + error_message + max_iterations no-tool finalization + error/empty 注入排空 + governance 异常保护；step37 完成 NANOBOT_LLM_TIMEOUT_S；`tool_events`/`fail_on_tool_error`/malformed_retry 未做 | `runner.py` 全量 | step30/32/37 ✅(主体) / step46(malformed_retry) / step52(tool_events/fail_on_tool_error) | ✅(主体) ⬜(malformed_retry/tool_events/fail_on_tool_error) |
| A9 | **运行时上下文提供器**：`RuntimeContextProvider` 每 turn 前动态解析上下文块并 append | step28 完成；step31 完成展示期移除 | `runtime_context.py`、`loop.py:_resolve_runtime_context_for_turn` | step28/31 | ✅ |
| A10 | **Workspace 安全模型**：`WorkspaceScopeResolver` + ContextVar 绑定 + loopback 门禁 | step28 完成 | `security/workspace_access.py` | step28 | ✅ |
| A11 | **Skills 体系**：SKILL.md frontmatter、availability 过滤、workspace 覆盖、disabled_skills、渐进加载 | step27 完成 | `agent/skills.py`、`agent/context.py` | step27 | ✅ |
| A12 | **Turn continuation + 隐藏历史**：隐形续跑、`_skip_user_persist`、`finalize_on_max_iterations`、HIDDEN_HISTORY_META 过滤 | step29/31 完成；`finalize_on_max_iterations` 已函数式化 | `session/turn_continuation.py`、`session/history_visibility.py` | step29/31 | ✅ |
| A13 | **调度与并发**：`_active_tasks` 跟踪、`_concurrency_gate`、CancelledError 泄漏防护 | step29 完成；step40 `_dispatch` 已用 `task_is_cancelling()` 免疫泄漏 | `loop.py:run/_dispatch`、`utils/cancellation.py` | step29/40 | ✅ |
| A14 | **FileStateStore / ExecSessionManager**：文件读/写去重追踪；shell 会话隔离 | step39 完成 file_state ContextVar 绑定（runner run() 中 bind/reset）；完整 FileStateStore/ExecSessionManager 未做 | `agent/tools/file_state.py`、`agent/tools/exec_session.py` | step39 ✅(contextvar绑定) / 未来候选(完整Store) | ✅(contextvar) ⬜(完整Store/ExecSessionManager) |
| A15 | **Session 历史回放增强**：`get_history` 增加 `extend_to_user`/`include_runtime_context`、`recent_message_start_index` 切片、user turn 对齐、空 assistant 过滤、`_command` 过滤、字段白名单 | step33 完成 | `session/manager.py:get_history`、`utils/helpers.py:recent_message_start_index` | step33 | ✅ |
| A16 | **Consolidation Replay Overflow 压缩**：`_replay_overflow_boundary` + `_consolidate_replay_overflow` + `estimate_session_prompt_tokens` + `maybe_consolidate_by_tokens(replay_max_messages=)` | step33 完成 | `agent/memory.py:Consolidator`、`loop.py:_state_build` | step33 | ✅ |
| A17 | **_run_agent_loop 提取**：核心循环提取为独立方法，返回 `(final_content, tools_used, messages, stop_reason, had_injections)` 元组 | step35 完成；step40 已扩展 hook_factories/turn_scopes 参数 | `loop.py:_run_agent_loop` | step35/40 | ✅ |
| A18 | **_persist_user_message_early 提前持久化 + _build_initial_messages 提取** | step34 完成 | `loop.py:_persist_user_message_early`、`_build_initial_messages` | step34 | ✅ |
| A19 | **_drain_pending 阻塞等待 subagent**：subagent 仍在运行时阻塞等待 `pending_queue.get(timeout=300)` | 已被 A2（step23）覆盖；step40 `_run_agent_loop` 提取后注入回调保留 | `loop.py:_run_agent_loop:_drain_pending` | step23 | ✅(已被A2覆盖) |
| A20 | **_sync_subagent_runtime_limits + self.max_iterations 属性**：loop 中同步 subagent max_iterations；`_build_agent_spec` 用 `self.max_iterations` 替代硬编码 5 | step36 完成；step38 from_config 已从 defaults.max_tool_iterations 读取 | `loop.py:_sync_subagent_runtime_limits`、`AgentLoop.max_iterations` | step36/38 | ✅ |
| A21 | **contextvar 绑定统一**：file_state ContextVar bind/reset | step39 完成（runner run() 中 `bind_file_states(FileStates())` + `reset_file_states`）；request_context/workspace_scope 由 runner 绑定 | `runner.py:run()` contextvar bind/reset | step39 | ✅ |
| A22 | **llm_timeout_s + runner_wall_llm_timeout_s + NANOBOT_LLM_TIMEOUT_S**：AgentRunSpec 传入 llm_timeout_s；runner 环境变量支持；流式超时加倍；TimeoutError 带 error_kind | step37 完成 | `loop.py:runner_wall_llm_timeout_s`、`runner.py:_request_model` | step37 | ✅ |
| A23 | **turn_scopes（ExitStack）+ hook_factories 分离**：`_run_agent_loop` 新增 hook_factories/turn_scopes 参数 + ExitStack；AgentLoop 分离 `self._hook_factories`；`_schedule_background` | step40 完成（429 tests） | `loop.py:TurnContext.turn_scopes`、`build_agent_turn_hook(registered_hook_factories=)` | step40 | ✅ |
| **A24** | **ephemeral 模式完整支持**：TurnContext.ephemeral/run_extra_hooks_for_ephemeral；_state_build/_state_save 条件跳过持久化/compact/consolidation；_state_respond 挂 _stop_reason | step41 已完成 | `loop.py:_run_agent_loop(ephemeral=)`、各 state 条件分支 | **step41** | ✅ |
| **A25** | **_assemble_outbound 提取 + MessageTool 抑制**：从 _state_respond 提取；支持 `mt._sent_in_turn` 抑制；meta 用 latency_ms 替代 tokens | step42 已完成 | `loop.py:_assemble_outbound` | **step42** | ✅ |
| **A26** | **process_direct 公共 API**：`process_direct(prompt, session_key, ephemeral, hooks, hook_factories, tools, persist_user_message, runtime)`；绕过 bus 直接走状态机；run_dream 标记 deprecated | step43 已完成 | `loop.py:process_direct`（第1969-2024行） | **step43**（依赖 step41） | ✅ |
| **A27** | **StateTraceEntry 状态追踪**：_process_message 中每个状态记录 trace（state/started_at/duration_ms/event/error） | step44 已完成 | `loop.py:StateTraceEntry`、`TurnContext.trace` | **step44** | ✅ |
| **A28** | **函数式参数 + 流式分段 + background_tasks**：`goal_continue_message` 闭包化（动态读 session.metadata）；`_wants_stream` 流式分段（stream_base_id/stream_segment）；`_background_tasks` 跟踪 + shutdown drain | goal_continue_message 为静态 str；`_schedule_background` 仅 create_task 无跟踪；无流式分段 | `loop.py:_goal_continue()` 闭包、`_wants_stream` | **step54** | ⬜ |
| **A29** | **runner _drop_malformed_tool_calls 元组 + malformed_retry**：返回 `(dropped, all_dropped, original_finish_reason)`；mutate response；all_dropped 时递归重试一次；仍失败降级无工具请求 | step46 已完成 | `runner.py:_drop_malformed_tool_calls`（第873-905行）、`_request_model(malformed_retry=)` | **step46** | ✅ |
| **A30** | **runner _request_finalization_retry + 辅助方法提取**：空响应重试耗尽后发独立无工具请求；`_merge_message_content`（block 合并）；`_build_request_kwargs`（集中构造）；`_append_final_message`/`_append_model_error_placeholder` | step47 已完成 finalization_retry；辅助方法提取留到 step58 | `runner.py:_request_finalization_retry`（第928-934行）、`_merge_message_content`（第108-123行） | **step47**(主体) / **step58**(辅助方法) | ✅(主体) ⬜(辅助方法) |
| **A31** | **runner hook.finalize_content + reasoning 提取**：用 `hook.finalize_content(context, response.content)` 替代直接 content；`extract_reasoning` 分离 reasoning；`emit_reasoning`/`emit_reasoning_end` 流式输出 | step48 已完成（一次性输出）；流式输出 step53 已完成 | `runner.py:extract_reasoning`（第397-409行）、`hook.finalize_content`（第515行） | **step48**(一次性) / **step53**(流式) | ✅ |
| **A32** | **runner usage 估算升级**：`estimate_prompt_tokens_chain(provider, model, messages, tools)` + `estimate_message_tokens` 替代 `chars//4`；`_usage_or_estimate`/`_usage_dict`/`_usage_total`/`_merge_usage`；usage 增加 total_tokens/provider_tokens/estimated_tokens | step49 已完成 | `runner.py:_estimate_response_usage`（第1027-1058行）、`_merge_usage`（第1083-1088行） | **step49** | ✅ |
| **A33** | **runner SSRF/workspace 安全检测**：`_is_ssrf_violation` + `_SSRF_BOUNDARY_NOTE` + `_ssrf_soft_payload`；`_is_workspace_violation` + 重复违规升级；`_classify_violation` 统一分类；重复外部查找阻断（external_lookup_counts） | step51 已完成 | `runner.py:_SSRF_MARKERS`（第1254-1266行）、`_classify_violation`（第1295-1333行） | **step51** | ✅ |
| **A34** | **runner fail_on_tool_error + tool_events**：工具错误时 fatal_error 终止 turn；每个工具调用记录 `{name, status, detail}` 事件 | step52 已完成 | `runner.py:AgentRunResult.tool_events`、`_execute_tools` 返回 `(results, events, fatal_error)` | **step52**（依赖 step50） | ✅ |
| **A35** | **runner Progress streaming + Thinking/reasoning 流**：`stream_progress_deltas` + `IncrementalThinkExtractor`（非流式请求也输出进度增量）；`on_thinking_delta` + `emit_reasoning` 增量推理流 | step53 已完成（progress_callback 模式）；on_thinking_delta 单独通道留到未来 | `runner.py:_request_model` progress_streaming 分支（第777-805行） | **step53**（依赖 step48） | ✅ |
| **A36** | **loop media 处理**：`_prepare_message_media` + `extract_documents` + `reference_non_image_attachments` + `image_placeholder_text`；`_should_extract_document_text`（依赖 channels_config） | 无 media 处理；_save_turn 无 image_url data: 替换（step45 已加 image_url 处理） | `loop.py:_prepare_message_media`、`_save_turn` image_url 处理 | **step56** | ⬜ |
| **A37** | **loop ModelRuntimeResolver 完整实现**：`set_model_preset`/`set_runtime_model`/`set_runtime_context_window`；`llm_runtime()` 方法；`model_preset` property+setter；provider_signature 热刷新 | 骨架有 LLMRuntime/ModelPreset；无动态切换/热刷新 | `agent/model_runtime.py:ModelRuntimeResolver` | **step55** | ⬜ |
| **A38** | **loop CronTurnCoordinator / automation turns / MCP**：`_cron_turns`/`_local_trigger_turns`/`_automation_turn_coordinators`/`_deferred_automation_turns`；`submit_cron_turn`/`submit_local_trigger_turn`；MCP 连接管理（`_connect_mcp`/`close_mcp`） | 无；dream 通过 `run_dream()` 独立路径 | `loop.py:_cron_turns`、`agent/cron_turns.py`、`agent/automation_turns.py` | 未来候选（依赖 harness CronService） | ⬜ |
| **A39** | **_save_turn 增强 + _state_command 持久化**：image_url data: 替换 + `_meta` 弹出 + `updated_at` 类型统一；shortcut 命令持久化 user+assistant（`_command` 标记） | step45 已完成 | `loop.py:_save_turn`、`_state_command` | **step45** | ✅ |
| **A40** | **runner _run_tool hook 生命周期 + _execute_tools 三元组**：`before_execute_tool`/`after_execute_tool`/`on_execute_tool_error` 标准 hook；`_execute_tool_batch` → `_execute_tools` 返回 `(results, events, fatal_error)`；`run()` 分离 CancelledError | step50 已完成（保留 _execute_tool_batch 名称） | `runner.py:_run_tool`、`_execute_tools` | **step50** | ✅ |
| **A41** | **TurnContext 字段重构 + 技术债清理**：移除 `result`/`error`/`summary`，改用直接字段 + `pending_summary`；`_state_run` 不再重建 ctx.result；`_build_agent_spec` 内联化评估 | TurnContext 有 result/error/summary；_state_run 重建 ctx.result（注释承认"ToolLoader.load 幂等"） | `loop.py:TurnContext`、`_state_run` | **step57**（依赖 step43） | ⬜ |
| **A42** | **runner 收尾对齐**：`_PERSISTED_MODEL_ERROR_PLACEHOLDER` 模型错误占位符；`is_tool_error_result` 工具错误识别；`_merge_message_content` block 合并；`_build_request_kwargs` 集中构造；`_append_final_message`/`_append_model_error_placeholder` 提取 | 部分已完成（finalization_retry）；辅助方法未提取 | `runner.py` 全量 | **step58** | ⬜ |
| **A43** | **loop 收尾对齐**：`_process_system_message` skip/extend_to_user 对齐；workspace_scope `for_turn` 替代 `for_message`；runtime_events 参数支持；`_dispatch` CLI 空响应无条件回空；`_request_context_for_turn` 重命名 | 部分对齐；仍有差异 | `loop.py:_process_system_message`、`workspace_scopes.for_turn` | **step59** | ⬜ |
| **A44** | **配置层扩展 + from_config 完整对齐**：新增 `channels_config`/`tools_config`/`web_config`/`exec_config`；`from_config` 新增 `provider_snapshot_loader`/`preset_snapshot_loader`/`configured_model_presets`/`restart_mode`/`image_generation_provider_configs`；AgentLoop.__init__ 新增 `workspace: Path` | from_config 雏形已完成；扩展字段未加 | `config/schema.py`、`loop.py:from_config` | **step60** | ⬜ |

---

## H. Harness 外圈（装配 / 配置 / 总线 / 通道 / 网关）— 降优先级

| ID | 缺口 | 现状（step40） | nanobot 参考 | 归属步骤 | 状态 |
|----|------|---------------|--------------|---------|------|
| H1 | **Config 配置系统**：schema + loader（`NANOBOT__` env、`${VAR}` 替换、迁移）；`agents.defaults` | step25 完成；step38 已接入 max_tool_iterations | `config/schema.py`、`config/loader.py` | step25/38 | ✅ |
| H2 | **Providers Registry / Factory / Fallback / Snapshot** | step22 完成 registry+factory+异常式 Fallback+Snapshot；自动探测/热刷新待后续 | `providers/registry.py`、`providers/factory.py` | step22/25 | ✅(异常式) |
| H3 | **装配 harness（from_config）**：`AgentLoop.from_config` 统一装配；outbound→session 镜像（`_deliver_to_channel`）；MessageTool 回调桥接；TokenUsageHook；健康检查 | step25 完成 `AgentLoop.from_config` 雏形；step38 已加 max_tool_iterations；step40 仍为极简 main.py 脚本式装配；outbound 镜像/MessageTool 桥接/TokenUsageHook/健康检查未做 | `cli/commands.py:_run_gateway` | step25/38 起步 / 未来候选(完整) | ✅(雏形) ⬜(完整) |
| H4 | **事件层**：typed outbound events + RuntimeEventBus | step26 完成 | `bus/outbound_events.py`、`bus/runtime_events.py` | step26 | ✅ |
| H5 | **Provider 重试引擎**：transient vs 不可重试分类、Retry-After 解析、retry_mode、on_retry_wait 心跳、去图重试、角色交替强制 | step30 完成主体；去图重试/角色交替强制未做 | `providers/base.py` | step30 | ✅(主体) ⬜(去图/角色交替) |
| H6 | **真实通道**：telegram/discord/slack/email/wecom/dingtalk/feishu 等 | 仅 cli | `channels/*` | 未来候选 | ⬜ |
| H7 | **安全**：SSRF 白名单、workspace sandbox、ingress 策略、loopback 门禁 | step28 完成 workspace sandbox + loopback 门禁；SSRF 白名单/ingress 策略未做（runner 侧 SSRF 检测见 A33） | `security/`、`webui/ingress_policy.py` | step28 | ✅(workspace) ⬜(SSRF/ingress) |
| H8 | **Session 键与可见性**：统一键 `unified:...` / `channel:chat_id`、history_visibility、automation 持久化 | step29 完成统一键 + history_visibility；automation 持久化未做 | `session/keys.py` | step29 | ✅ |
| H9 | **Cron / 触发器 / 自动化**：CronService（dream/heartbeat/bound）、LocalTriggerStore、automation_turns | 仅 `_dream_loop`（step15）+ `run_dream()`（step40 loop.py）；无 CronService | `cron/`、`triggers/` | step61（依赖 A26 process_direct） | ⬜ |
| H10 | **日志**：loguru 统一日志替代散落 print/logging | 大量 logging（部分已收敛） | `utils/logging_bridge.py` | 任意步骤顺手 | ⬜ |
| H11 | **Gateway HTTP API / WebUI / SDK / OS 服务**：产品与部署层 | 无；step40 main.py 为极简脚本 | `api/`、`webui/`、`nanobot.py`、`gateway/service.py` | 不做（记录备查） | ⬜ |
| H12 | **进程生命周期管理（ManagedProcessRuntime）**：跨平台后台启动/停止/重启/日志跟随；state file + filelock；PID 复用防护（creation time）；Windows CTRL_BREAK+taskkill / POSIX SIGTERM+process group | 无；step40 为前台脚本 | `process_runtime.py:ManagedProcessRuntime`、`gateway/runtime.py:GatewayRuntime` | step63 | ⬜ |
| H13 | **CLI 命令组（Typer）**：gateway(前台/后台)/status/logs/stop/restart/install-service/uninstall-service | 无 | `cli/gateway.py`、`cli/commands.py` | step63 | ⬜ |
| H14 | **系统服务安装**：systemd user service / macOS LaunchAgent | 无 | `gateway/service.py:GatewayServiceInstaller` | step63 | ⬜ |
| H15 | **Heartbeat 机制**：HEARTBEAT.md 活跃任务检测 → process_direct → LLM evaluator 判定是否通知 → 通道投递 | 无 | `cli/commands.py:on_cron_job(heartbeat)` | step64（依赖 H9 + A26） | ⬜ |
| H16 | **WebUI 集成**：bundle 准备、webui channel、WebuiTurnCoordinator、浏览器自动打开 | 无 | `channels/webui.py`、`webui_turn_coordinator.py` | 不做（记录备查） | ⬜ |
| H17 | **SDK 门面（Nanobot 类）**：`run()`/`run_streamed()`/`stream()` + SessionClient/MemoryClient/RuntimeClient | 无 | `nanobot.py:Nanobot` | 不做（记录备查） | ⬜ |

---

## 路线衔接说明

- 每完成一项：更新状态 ✅，并在 `roadmap.md` 的对应 step 标注完成。
- 步骤内文件 import 规则：`step(N).` → `step(N+1).`，从上一 step fork。
- 所有测试禁止真实 API Key（AGENTS.md 原则 0）。
- **当前进度：step40 已完成（429 tests），step41 规划中**（ephemeral 模式）。
- **agent 综合对齐度：~78%**（step35 为 72%，step36-40 五个 step 提升 6%）。
- **对齐依据：** 所有 A24+ / H12+ 条目均来自 `align.md` 的 step40 vs nanobot 缺口分析，每条可追溯到 nanobot 具体文件和行号。
- **建议立即启动 step41（ephemeral）**：它是 process_direct（step43）和 harness CronService（step58）的前置依赖，改动范围可控（TurnContext 加字段 + 三个 state 加条件分支）。
- **runner 阶段建议在 loop 阶段一（step41-45）完成后启动**：runner 改动不依赖 loop，但 malformed_retry（step46）等需要充分回归测试。
