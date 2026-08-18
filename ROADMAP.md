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
| 31 | 公共历史 + 运行时上下文展示期移除（A12 下半场） | runtime_context.py + session/manager.py + consolidation.py | 300 |
| 32 | Runner Finalization 对齐：max_iterations 无工具收尾 + error/empty 注入排空 + governance 异常保护 | runner.py + loop.py | 297 |
| 33 | Session 历史回放增强 + Replay Overflow 压缩（A15 + A16） | session/manager.py + consolidation.py + loop.py + helpers.py | ~322 |
| 34 | _persist_user_message_early 提前持久化 + _build_initial_messages 提取（A18） | loop.py + session/manager.py | 351 |
| 35 | _run_agent_loop 提取为独立方法 + max_iterations stream budget（A17） | loop.py + session/turn_continuation.py | 365 |
| 36 | self.max_iterations 属性 + _sync_subagent_runtime_limits | loop.py | ~370 |
| 37 | llm_timeout_s + runner_wall_llm_timeout_s + NANOBOT_LLM_TIMEOUT_S | loop.py + runner.py + goal_state.py | ~380 |
| 38 | 配置层接入 max_tool_iterations（from_config 读取 defaults.max_tool_iterations） | loop.py + config/schema.py | ~390 |
| 39 | file_state ContextVar 绑定（runner run() 中 bind/reset） | runner.py + tools/file_state.py | ~410 |
| 40 | turn_scopes + hook_factories 两层分离 + _schedule_background | loop.py + hook.py | **429** |
| 41 | ephemeral 模式 + run_extra_hooks_for_ephemeral | loop.py + context.py | 406 |
| 42 | _assemble_outbound 提取 + MessageTool 抑制 | loop.py + tools/message.py | 421 |
| 43 | process_direct 公共 API | loop.py | 431 |
| 44 | StateTraceEntry 状态追踪 | loop.py + context.py | 438 |
| 45 | _save_turn 增强 + _state_command 持久化 | loop.py + session/manager.py | 445 |
| 46 | _drop_malformed_tool_calls 元组 + malformed_retry | runner.py | 453 |
| 47 | _request_finalization_retry + 空响应处理 | runner.py | 458 |
| 48 | hook.finalize_content + reasoning 提取 | runner.py | 464 |
| 49 | usage 估算升级 | runner.py | 474 |
| 50 | _run_tool hook 生命周期 + 三元组返回 | runner.py | 481 |
| 51 | SSRF/workspace 安全检测 | helpers.py + runner.py | 492 |
| 52 | fail_on_tool_error + tool_events | runner.py | 498 |
| 53 | progress streaming + thinking 流 | runner.py | 504 |

---

# 建议补齐路线（基于 step53 vs nanobot 缺口分析，详见 `align.md`）

> **优先级原则：agent 核心 > runner 健壮性 > harness 外圈。** 每步只做一个对齐点，独立可测试。
> step53 agent 综合对齐度 ~97%（step40 为 78%）。
> 阶段四（step54–57）loop 高级，预计 +2%；阶段五（step58–60）agent 收尾，预计 +1% 达到 100%；阶段六（step61+）harness。

---

## 阶段一：loop 核心功能对齐（step41–45）✅ 已完成

> 目标：补齐 ephemeral/process_direct/outbound 组装，为 harness 迁移铺路。
> 实际完成：406→445 tests，agent 对齐度 +10%

### Step 41 — ephemeral 模式 + run_extra_hooks_for_ephemeral 🟡 规划中

**主题：** loop 不持久化 turn——为 dream/heartbeat/一次性查询铺路

**优先级：** 高（process_direct 和 harness dream 迁移的前置条件）

| 改进 | 说明 |
|------|------|
| `loop.py:TurnContext` | 新增 `ephemeral: bool`、`run_extra_hooks_for_ephemeral: bool` 字段 |
| `loop.py:_run_agent_loop` | 签名增加 `ephemeral`、`run_extra_hooks_for_ephemeral` 参数 |
| `loop.py:_state_build` | ephemeral 时跳过 consolidation |
| `loop.py:_state_save` | ephemeral 时跳过持久化 + enforce_file_cap + 后台 consolidation |
| `loop.py:_state_respond` | ephemeral 时挂 `_stop_reason` 元数据 |

**不做什么：** 不迁移 dream（留待 harness 阶段）；只搭 ephemeral 基础设施。

**预期测试：** 429 + ~12 = ~441 passed

**导入：** 从 step40 fork，import `step40.` → `step41.`

---

### Step 42 — _assemble_outbound 提取 + MessageTool 抑制 ⬜ 待规划

**主题：** loop 出站消息组装——提取方法 + MessageTool 直接发送抑制

**优先级：** 中（结构对齐，为 MessageTool.set_send_callback 铺路）

| 改进 | 说明 |
|------|------|
| `loop.py:_assemble_outbound` | 从 `_state_respond` 提取为独立方法 |
| MessageTool 抑制 | 支持 `mt._sent_in_turn` 标记，避免 MessageTool 已直接发送后重复出站 |
| meta 字段 | 用 `latency_ms` 替代 `tokens` |

**对应 nanobot：** loop.py `_assemble_outbound` 方法

**预期测试：** ~441 + ~8 = ~449 passed

---

### Step 43 — process_direct 公共 API ⬜ 待规划

**主题：** loop 直接处理消息入口——为 harness dream/heartbeat 从 run_dream 迁移铺路

**优先级：** 高（harness CronService 引入的前置条件）

| 改进 | 说明 |
|------|------|
| `loop.py:process_direct` | 新增公共方法：`process_direct(prompt, session_key, ephemeral, hooks, hook_factories, tools, persist_user_message, runtime)` |
| 内部复用 | 复用 `_dispatch` / `_process_message` 状态机，绕过 bus 入站 |
| `run_dream` | 标记为 deprecated，内部可委托给 `process_direct(ephemeral=True)` |

**对应 nanobot：** loop.py 第 1969–2024 行

**依赖：** step41(ephemeral)

**预期测试：** ~449 + ~12 = ~461 passed

---

### Step 44 — StateTraceEntry 状态追踪 ⬜ 待规划

**主题：** loop 可观测性——每个状态记录 trace（state/started_at/duration_ms/event/error）

**优先级：** 低（纯可观测性，不影响功能）

| 改进 | 说明 |
|------|------|
| `loop.py:TurnContext` | 新增 `trace: list[StateTraceEntry]` 字段 |
| `loop.py:_process_message` | 每个状态转换前记录 started_at，转换后记录 duration_ms 和 event/error |
| `StateTraceEntry` | 新增 dataclass：state, started_at, duration_ms, event, error |

**预期测试：** ~461 + ~6 = ~467 passed

---

### Step 45 — _save_turn 增强 + _state_command 持久化 ⬜ 待规划

**主题：** loop 持久化完善——image_url 处理 + shortcut 命令持久化 + updated_at 类型统一

**优先级：** 中（持久化兼容性）

| 改进 | 说明 |
|------|------|
| `loop.py:_save_turn` | 增加 image_url data: 替换为 `image_placeholder_text`；弹出 `_meta`/`RUNTIME_CONTEXT_MESSAGE_META`；`updated_at` 类型统一 |
| `loop.py:_state_command` | shortcut 命令持久化 user+assistant（`_command` 标记），有 `is_user_turn` 判定 |

**预期测试：** ~467 + ~10 = ~477 passed

---

## 阶段二：runner 健壮性对齐（step46–50）✅ 已完成

> 目标：补齐 malformed_retry/finalization_retry/finalize_content/usage，消除 wedge 风险。
> 实际完成：453→481 tests，agent 对齐度 +8%

### Step 46 — _drop_malformed_tool_calls 元组 + malformed_retry ⬜ 待规划

**主题：** runner 健壮性——畸形工具调用返回元组 + 递归重试路径

**优先级：** 高（防止畸形 tool_call 永久 wedge session；当前 all_dropped 时只追加提示后 continue，不重新请求）

| 改进 | 说明 |
|------|------|
| `runner.py:_drop_malformed_tool_calls` | 返回 `(dropped, all_dropped, original_finish_reason)` 元组，直接 mutate `response.tool_calls` 和 `finish_reason` |
| `runner.py:_request_model` | 增加 `malformed_retry: bool` 参数；all_dropped 且原 finish_reason 为 tool_calls 时递归重试一次 |
| `runner.py:_malformed_tool_call_retry_messages` | 新增方法，构造重试提示消息（保留原 assistant 文本） |
| `runner.py:_request_no_tools` | malformed_retry 仍失败时降级为无工具请求 |

**对应 nanobot：** runner.py 第 840–871 行

**预期测试：** ~477 + ~10 = ~487 passed

---

### Step 47 — _request_finalization_retry + 辅助方法提取 ⬜ 待规划

**主题：** runner 空响应处理——重试耗尽后发独立 finalization 请求 + 代码结构对齐

**优先级：** 中（当前空响应耗尽后直接用 fallback 文案，nanobot 多一次无工具请求）

| 改进 | 说明 |
|------|------|
| `runner.py:_request_finalization_retry` | 新增方法：空响应重试耗尽后发一次无工具请求 |
| `runner.py:_finalization_retry_messages` | 新增方法，构造 finalization 重试消息 |
| `runner.py:_merge_message_content` | 新增静态方法，支持 block 列表合并（替代字符串拼接） |
| `runner.py:_build_request_kwargs` | 新增方法，集中构造请求 kwargs（含 reasoning_effort） |
| `runner.py:_append_final_message` / `_append_model_error_placeholder` | 提取为静态方法，防重复追加 |

**对应 nanobot：** runner.py 第 928–940、108–123、691–709、1353–1372 行

**预期测试：** ~487 + ~10 = ~497 passed

---

### Step 48 — hook.finalize_content + reasoning 提取 ⬜ 待规划

**主题：** runner 内容最终化——hook 最终化回调 + 推理内容分离与流式输出

**优先级：** 中（A7 剩余部分 + reasoning 支持，影响输出质量）

| 改进 | 说明 |
|------|------|
| `runner.py:_run_loop` | 用 `hook.finalize_content(context, response.content)` 替代直接用 `response.content` |
| `runner.py` | 导入 `extract_reasoning`，分离 `reasoning_content`/`thinking_blocks` 和 `content` |
| `runner.py` | 新增 `emit_reasoning` / `emit_reasoning_end` hook 调用 + `streamed_reasoning` 跟踪 |
| `runner.py:_try_finalize_after_max_iterations` | 成功后也调用 `hook.finalize_content` |

**对应 nanobot：** runner.py 第 397–409、515、978 行

**预期测试：** ~497 + ~12 = ~509 passed

---

### Step 49 — usage 估算升级 ⬜ 待规划

**主题：** runner token 估算——provider 感知的链式估算替代简单 chars//4

**优先级：** 低（usage 仅用于预算簿记，不影响核心功能）

| 改进 | 说明 |
|------|------|
| `runner.py:_estimate_response_usage` | 替换 `_estimate_usage`，使用 `estimate_prompt_tokens_chain(provider, model, messages, tools)` + `estimate_message_tokens` |
| `runner.py:_usage_or_estimate` | 新增方法：优先用真实 usage，缺失时估算 |
| `runner.py:_usage_dict` / `_usage_total` / `_merge_usage` | 新增 usage 工具方法 |
| usage 字典 | 增加 `total_tokens`、`provider_tokens`、`estimated_tokens` 键 |

**对应 nanobot：** runner.py 第 1011–1088 行

**预期测试：** ~509 + ~6 = ~515 passed

---

### Step 50 — _run_tool hook 生命周期 + _execute_tools 三元组 ⬜ 待规划

**主题：** runner 工具执行——标准 hook 生命周期 + 三元组返回 + CancelledError 分离

**优先级：** 中（hook 体系完整性 + fail_on_tool_error 前置）

| 改进 | 说明 |
|------|------|
| `runner.py:_run_tool` | 增加 `before_execute_tool`/`after_execute_tool`/`on_execute_tool_error` 标准 hook |
| `runner.py:_execute_tool_batch` → `_execute_tools` | 返回 `(results, events, fatal_error)` 三元组 |
| `runner.py:run()` | 分离 `CancelledError`（不调 on_error）和 `Exception`（调 on_error） |

**预期测试：** ~515 + ~12 = ~527 passed

---

## 阶段三：runner 安全与高级特性（step51–53）✅ 已完成

> 目标：补齐 SSRF/workspace 安全检测 + fail_on_tool_error + 流式 thinking。
> 实际完成：492→504 tests，agent 对齐度 +4%

### Step 51 — SSRF/workspace 安全检测 ⬜ 待规划

**主题：** runner 安全边界——SSRF 阻断 + workspace 违规 + 重复违规升级

**优先级：** 低（安全增强，需 security 基础设施）

| 改进 | 说明 |
|------|------|
| `runner.py:_is_ssrf_violation` | 检测工具返回中的 SSRF markers |
| `runner.py:_SSRF_BOUNDARY_NOTE` + `_ssrf_soft_payload` | 硬安全阻断，返回不可重试的工具错误 |
| `runner.py:_is_workspace_violation` | 检测 workspace 违规 markers + 重复违规升级 |
| `runner.py:_classify_violation` | 统一分类安全边界失败 |
| `runner.py:external_lookup_counts` | 重复外部查找阻断 |

**对应 nanobot：** runner.py 第 1254–1333 行

**预期测试：** ~527 + ~15 = ~542 passed

---

### Step 52 — fail_on_tool_error + tool_events ⬜ 待规划

**主题：** runner 工具错误处理——工具错误终止 turn + 事件追踪

**优先级：** 低

| 改进 | 说明 |
|------|------|
| `runner.py:AgentRunSpec` | 新增 `fail_on_tool_error` 字段 |
| `runner.py:AgentRunResult` | 新增 `tool_events` 列表 |
| `runner.py:_execute_tools` | 工具错误时 fatal_error 终止 turn；每个工具调用记录 `{name, status, detail}` |

**依赖：** step50(三元组)

**预期测试：** ~542 + ~10 = ~552 passed

---

### Step 53 — progress streaming + thinking 流 ⬜ 待规划

**主题：** runner 流式增强——非流式请求进度增量 + 推理内容流式输出

**优先级：** 低

| 改进 | 说明 |
|------|------|
| `runner.py:stream_progress_deltas` | `IncrementalThinkExtractor` 非流式请求也输出进度增量 |
| `runner.py:on_thinking_delta` | 增量推理流 + `emit_reasoning` |

**依赖：** step48(reasoning)

**预期测试：** ~552 + ~10 = ~562 passed

---

## 阶段四：loop 高级特性（step54–57，预计 +2% agent 对齐度）

> 目标：补齐 loop 动态参数、模型运行时、多模态、技术债清理，agent 对齐度 ~99%。

### Step 54 — 函数式参数 + 流式分段 + background_tasks 🟡 规划中

**主题：** loop 动态参数——goal_continue_message 闭包化 + 流式分段 + 后台任务跟踪

**优先级：** 中

| 改进 | 说明 |
|------|------|
| `loop.py:goal_continue_message` | 支持 `str \| Callable[[], str \| None]`，闭包 `_goal_continue()` 动态读取 session.metadata |
| `loop.py:_wants_stream` | `stream_base_id`/`stream_segment` 分段流，支持大响应分段交付 |
| `loop.py:_background_tasks` | `_schedule_background` 登记到列表 + shutdown drain，避免后台任务泄漏 |
| `runner.py:goal_continue_message` | 同步支持函数式参数 |

**对应 nanobot：** loop.py `_goal_continue()`、`_wants_stream()`、`_background_tasks`

**预期测试：** 504 + ~10 = ~514 passed

---

### Step 55 — ModelRuntimeResolver 完整实现 🟡 规划中

**主题：** loop 动态模型——provider_signature 热刷新 + resolve_override + 动态模型切换

**优先级：** 低（A1 剩余）

| 改进 | 说明 |
|------|------|
| `loop.py:set_model_preset`/`set_runtime_model`/`set_runtime_context_window` | 动态切换模型 preset/runtime/context_window |
| `loop.py:llm_runtime()` | 运行时解析 LLMRuntime，替代静态 spec 参数 |
| provider_signature 热刷新 | 运行时 provider/preset 刷新，支持配置热更新 |
| `model_preset` property+setter | 动态模型 preset 访问 |

**对应 nanobot：** agent/model_runtime.py `ModelRuntimeResolver`

**预期测试：** ~514 + ~12 = ~526 passed

---

### Step 56 — media 处理 🟡 规划中

**主题：** loop 多模态——消息媒体准备 + 文档提取 + image 占位

**优先级：** 低（依赖 channels_config）

| 改进 | 说明 |
|------|------|
| `loop.py:_prepare_message_media` | 消息媒体准备（image_url 处理、文档提取） |
| `loop.py:extract_documents` + `reference_non_image_attachments` | 文档提取 + 非图片附件引用 |
| `loop.py:image_placeholder_text` + `_should_extract_document_text` | image 占位文本 + 文档文本提取判定 |
| `loop.py:_save_turn` image_url 处理 | 持久化时 image_url data: 替换为占位文本 |

**对应 nanobot：** loop.py `_prepare_message_media`、`extract_documents`

**预期测试：** ~526 + ~12 = ~538 passed

---

### Step 57 — TurnContext 字段重构 + 技术债清理 🟡 规划中

**主题：** loop 技术债——移除 result/error/summary + _state_run 不再重建 ctx.result

**优先级：** 低（重构，不改变功能）

| 改进 | 说明 |
|------|------|
| `loop.py:TurnContext` | 移除 `result`/`error`/`summary`，改用直接字段 + `pending_summary` |
| `loop.py:_state_run` | 不再重建 ctx.result，直接解构 `_run_agent_loop` 返回元组 |
| `loop.py:_build_agent_spec` | 评估是否内联到 `_run_agent_loop`（nanobot 内联） |
| `loop.py:_process_message` 异常 | 对齐 nanobot：直接 raise（由 `_dispatch` 捕获），而非构建 OutboundMessage break |

**依赖：** step43(process_direct)

**预期测试：** ~538 + ~8 = ~546 passed

---

## 阶段五：agent 收尾对齐（step58–60，预计 +1% 达到 100%）

> 目标：补齐 agent 剩余小缺口，agent 综合对齐度达到 100%。

### Step 58 — runner 收尾对齐 🟡 规划中

**主题：** runner 剩余对齐——模型错误占位符 + 工具错误识别 + 辅助方法提取

**优先级：** 低

| 改进 | 说明 |
|------|------|
| `runner.py:_PERSISTED_MODEL_ERROR_PLACEHOLDER` | 模型错误时持久化占位符，避免空 assistant 消息 |
| `runner.py:is_tool_error_result` | 工具返回值中识别错误结果（统一错误检测入口） |
| `runner.py:_merge_message_content` | 支持 block 列表合并（替代字符串拼接） |
| `runner.py:_build_request_kwargs` | 集中构造请求 kwargs（含 reasoning_effort） |
| `runner.py:_append_final_message` / `_append_model_error_placeholder` | 提取为静态方法，防重复追加 |

**对应 nanobot：** runner.py 第 108–123、691–709、1353–1372 行

**预期测试：** ~546 + ~8 = ~554 passed

---

### Step 59 — loop 收尾对齐 🟡 规划中

**主题：** loop 剩余对齐——系统消息处理 + workspace 范围 + 运行时事件参数

**优先级：** 低

| 改进 | 说明 |
|------|------|
| `loop.py:_process_system_message` | `skip = 1 + len(history)`（替代 `len(initial_messages)`）；`extend_to_user=is_subagent` |
| `loop.py:workspace_scope` | `for_turn(channel, message_metadata, session_metadata)` 替代 `for_message` |
| `loop.py:runtime_events` | 支持传入参数 + `ensure_runtime_event_publisher` |
| `loop.py:_dispatch` CLI 空响应 | 对齐 nanobot：无条件回空消息（移除 `internal_continuation_pending` 条件） |
| `loop.py:_request_context_for_turn` | 方法重命名（从 `_build_turn_request_context`） |

**对应 nanobot：** loop.py `_process_system_message`、`workspace_scopes.for_turn`

**预期测试：** ~554 + ~8 = ~562 passed

---

### Step 60 — 配置层扩展 + from_config 完整对齐 🟡 规划中

**主题：** 配置层——channels_config/tools_config/web_config/exec_config + from_config 完整对齐

**优先级：** 低

| 改进 | 说明 |
|------|------|
| `config/schema.py` | 新增 `channels_config`/`tools_config`/`web_config`/`exec_config` 字段 |
| `loop.py:from_config` | 新增 `provider_snapshot_loader`/`preset_snapshot_loader`/`configured_model_presets`/`restart_mode`/`image_generation_provider_configs` |
| `loop.py:AgentLoop.__init__` | 新增独立 `workspace: Path` 参数 |
| `config/loader.py` | 支持新增配置字段的加载和验证 |

**对应 nanobot：** config/schema.py、loop.py `from_config`

**预期测试：** ~562 + ~10 = ~572 passed

---

## 阶段六：harness 对齐（step61+，agent 已 100%）

| Step | 主题 | 前置条件 |
|------|------|----------|
| **61** | CronService 引入（dream 从 run_dream 迁移到 process_direct(ephemeral=True)） | step43 |
| **62** | ChannelManager 参数扩展（session_manager/cron_service/local_trigger_store） | step61 |
| **63** | ManagedProcessRuntime + CLI gateway 命令组 | 无 |
| **64** | Heartbeat + WebUI + SDK 门面 | step61 |

---

## 未来候选（不纳入主路线）

| 主题 | 说明 |
|------|------|
| 真实通道 | telegram/discord/slack/email/wecom/dingtalk/feishu |
| loguru 统一日志 | 替代散落 print/logging |
| AgentRunSpec runtime 对象化 | 用 `runtime: LLMRuntime` 替代分散参数（根本性架构差异，风险高，建议维持现状） |
| `_MAX_GOAL_CONTINUATION_ROUNDS` 移除 | 学习版安全护栏，建议保留 |

---

## 设计原则

1. **最小增量** — 每步只改最少的文件，独立可测试
2. **向后兼容** — AgentRunSpec、AgentLoop 接口只加可选字段
3. **可拆分** — 复杂功能跨步骤，步间可通过 fork + import 变更串联
4. **测试先行** — 每步增加相应测试，不破坏原有测试
5. **原理先行** — 每步实现前先参考 nanobot，分析原理、选择方案、解释为什么、方案利弊，再决策
6. **核心优先** — agent 循环 / 上下文 / 记忆 / 压缩 优先于 harness 外圈和产品层
7. **对齐有据** — 每个对齐点在 `align.md` 中有对应条目，不做无依据的"看起来像"对齐
