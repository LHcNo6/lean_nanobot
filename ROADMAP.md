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

---

# 建议补齐路线（基于 step21 vs nanobot 缺口分析）

> 详细缺口清单见根目录 `todolist.md`（A1–A14 / H1–H11）。
> 路线以 **agent 核心正确性优先**（注入、持久化净化），其次 harness 地基（config、事件层），
> 最后能力扩展（skills、安全、reasoning）；产品/部署层（HTTP API、WebUI、MCP）降为未来候选。

---

## Step 22 — Providers Registry & Factory + Fallback（A1 + H2）✅ 已完成

**主题：** provider 注册/匹配/异常式回退；顺带把静态 `Runtime` 升级为 `LLMRuntime` 骨架 + model_presets

| 改进 | 说明 |
|------|------|
| `providers/registry.py` | `ProviderSpec` dataclass + ~6 条目（openai/deepseek/dashscope/openrouter/ollama/custom）+ `find_by_name` |
| `providers/factory.py` | `make_provider(settings)`，模型名关键词匹配 |
| `providers/fallback_provider.py` | 异常捕获式逐级回退（复用 `_StreamGuard` 已发 delta 不重试） |
| `llm.py` | `Runtime` → `LLMRuntime`（context_window/generation/model_preset/signature），loop 从 runtime 反推 replay budget |

**导入：** 从 step21 fork，import `step21.` → `step22.`

---

## Step 23 — Mid-turn Injection 打通 + Subagent 系统消息通道（A2 + A3 + A6）✅ 已完成（388 tests）

**主题：** 修复"注入死代码"——subagent 回包应在 turn 内注入而非排队成独立 turn

| 改进 | 说明 |
|------|------|
| `loop.py:_state_run` | `injection_callback` 接到 pending_queue；子代理未结束时阻塞等待注入（对齐 `_drain_pending`） |
| `runner.py` | `_has_injection_content`、`_MAX_INJECTIONS_PER_TURN` 参数、`allow_goal_continue` |
| `loop.py` | `channel=="system"` 分支：subagent 回包 `current_role="assistant"`、按 `subagent_task_id` 去重、前置持久化 |
| `TurnContext` | 补 turn_id / runtime / on_progress / on_stream / on_stream_end / pending_queue 字段 |

**已落地：** `_dispatch` 重写（identity 弹栈 + re-publish）、`_drain_pending` 阻塞注入、
`_process_system_message` 去重持久化标记、`build_messages(current_role)` 角色交替 merge、
`SpawnTool` 透传 session_key。测试 388 全绿（详见 `step23/step23.md`）。

**导入：** 从 step22 fork，import `step22.` → `step23.`

---

## Step 24 — Session 持久化净化 + Checkpoint 恢复（A4 + A5）✅ 已完成（411 tests）

**主题：** 防止畸形消息污染历史；崩溃后可恢复进行中的 turn

| 改进 | 说明 |
|------|------|
| `loop.py:_save_turn` | 丢弃空 assistant、校验并丢弃孤儿 tool result、`max_tool_result_chars`（默认 16000）截断、list 块净化（空补占位）、latency 打在最后 assistant 消息 |
| `loop.py` | `_set/_restore/_clear_runtime_checkpoint`（metadata 持久化 + `_checkpoint_message_key` overlap 去重 + pending call 补 interrupted 结果）；`_state_restore`/`_process_system_message` 恢复点；system 路径 skip 改为 `len(initial_messages)`（修正旧实现丢 subagent 轮末回复） |
| `runner.py` | `AgentRunSpec.checkpoint_callback` + `_emit_checkpoint`，`awaiting_tools`/`tools_completed`/`final_response`（含注入续跑路径）三语义发射点 |
| 测试 | `tests/test_persistence.py`（pytest，23 个）：净化 10 + checkpoint 7 + runner 发射 4 + 集成 2，全部 mock provider |

**导入：** 从 step23 fork，import `step23.` → `step24.`；测试改为 pytest（新增），回归仍走 `python -m unittest step24.test`（388）

---

## Step 25 — Pydantic 配置系统（H1）✅ 已完成（451 tests）

**主题：** nanobot config/schema + loader 最小集（`NANOBOT_` env 前缀 + JSON 文件）

| 改进 | 说明 |
|------|------|
| `config/schema.py` | `Base`（camel/snake 双写）+ `Config`（agents.defaults / providers / channels / model_presets）+ `resolve_preset` / `get_provider*` 查询（走 step22 registry） |
| `config/loader.py` | `load_config`/`save_config`/`${VAR}` 替换/迁移；手写 `NANOBOT_` env 解析（`__` 嵌套），文件优先、env 补缺（不用 pydantic-settings） |
| 接入 | main.py 删全部硬编码；factory 双路分发（Config / ProviderSettings，388 回归零改动）；`Tool.config_cls()` 落地（`resolve_tool_config` + EchoTool 演示）；`AgentLoop.from_config` 装配雏形；ToolContext 拿到真实 config/workspace |
| 测试 | `tests/test_config.py`（pytest，40 个）：schema/loader/工厂 Config 路径/from_config/tool 配置，全部构造数据 |

**导入：** 从 step24 fork，import `step24.` → `step25.`；回归 `python -m unittest step25.test`（388）+ `pytest step25/tests`（63）

---

## Step 26 — 事件层：typed outbound events + RuntimeEventBus（H4 + H3）✅ 已完成（388 tests + pytest 103）

**主题：** 为真实通道铺路——progress / retry_wait / stream end / turn 生命周期事件

| 改进 | 说明 |
|------|------|
| `bus/outbound_events.py` | ProgressEvent / RetryWaitEvent / StreamEndEvent / StreamedResponseEvent / TurnEndEvent / GoalStatusEvent |
| `bus/runtime_events.py` | 进程内 pub/sub：session_turn_started / run_status_changed / turn_completed |
| `bus/progress.py` | `build_bus_progress_callback` 发布 ProgressEvent |
| `loop.py` | on_retry_wait 回调接入 provider 重试心跳 |

**导入：** 从 step25 fork，import `step25.` → `step26.`

---

## Step 27 — Skills 加载器（A11）✅ 已完成（388 + pytest 142）

**主题：** SKILL.md frontmatter + 可用性过滤 + 渐进加载 + 参考注入

| 改进 | 说明 |
|------|------|
| `skills/loader.py` | `SkillsLoader`（workspace 覆盖内置 / `requires: bins/env` 可用性过滤 / `disabled_skills` / frontmatter 剥离 / `build_skills_summary` 渐进摘要 / `get_always_skills` 全量注入） |
| `context.py` | ContextBuilder 注入 `# Active Skills`（always + `skill_names` 显式）+ `# Skills` 摘要块；`from_config` 贯通 `disabled_skills` |
| 测试 | `tests/test_skills.py`（pytest，39 个，全构造数据）；回归 388 unittest + 103 pytest 全绿 |

**导入：** 从 step26 fork，import `step26.` → `step27.`

---

## Step 28 — Workspace 安全模型 + 运行时上下文（A10 + A9 + H7）

**主题：** 工具的文件访问权限模型与每 turn 动态上下文块

| 改进 | 说明 |
|------|------|
| `security/workspace_access.py` | `WorkspaceScopeResolver`（restrict_to_workspace / sandbox / access_mode）+ ContextVar 绑定 + loopback 门禁 |
| `runtime_context.py` | `RuntimeContextProvider` 注册与解析、`append_runtime_context` |
| `context.py:ToolContext` | 工具拿到真实 workspace/config，替代 `workspace=""`、`config=None` |

**导入：** 从 step27 fork，import `step27.` → `step28.`

---

## Step 29 — Turn continuation + 隐藏历史 + 调度并发（A12 + A13 + H8）

**主题：** 隐形续跑、可见性过滤、任务跟踪与并发门控

| 改进 | 说明 |
|------|------|
| `session/turn_continuation.py` | 隐形续跑（跨 max_iterations 12 轮）、`_skip_user_persist`、`finalize_on_max_iterations`、`should_persist_user_message` |
| `session/history_visibility.py` | HIDDEN_HISTORY_META 过滤（get_history / context / 持久化） |
| `loop.py` | `_active_tasks` 跟踪、`_concurrency_gate` 信号量、CancelledError 泄漏防护 |
| `session/keys.py` | 统一键 `unified:...` / `channel:chat_id` |

**导入：** 从 step28 fork，import `step28.` → `step29.`

---

## Step 30 — Reasoning + Hook 工厂 + Runner 健壮性收敛（A7 + A8 + H5）

**主题：** 推理流式输出、按 turn 的 hook 工厂、LLM 错误语义收敛

| 改进 | 说明 |
|------|------|
| `runner.py` | `extract_reasoning` / thinking_blocks / `emit_reasoning`；`should_execute_tools`（refusal/content_filter/error 禁工具）；usage 估算回退；`tool_events`；arrearage/配额识别；max_iterations 后 finalization |
| `hook.py` | `AgentTurnHookSpec`/`build_agent_turn_hook`、`AgentProgressHook`、`before/after_execute_tool`、`on_execute_tool_error`、`finalize_content`、tool_hint |
| `provider.py` | Retry-After 解析、`retry_mode=standard/persistent`、角色交替强制 |

**导入：** 从 step29 fork，import `step29.` → `step30.`

---

## 未来候选

| Step | 主题 | 说明 |
|------|------|------|
| 31+ | Gateway & HTTP API | OpenAI 兼容 API（`POST /v1/chat/completions` SSE + `/v1/models` + `/health`）；产品层，待 agent 核心稳定后规划 |
| 31+ | MCP Integration | `mcp_<server>_<tool>` stdio/HTTP 客户端；需要 config 与工具注册稳定 |
| 31+ | 真实通道 | telegram / discord / slack / email / wecom / dingtalk / feishu（含 send_delta stream_id/resuming、send_reasoning_delta、transcribe_audio、每通道 send_max_retries、entry_points 插件发现） |
| 31+ | Cron / Triggers / 自动化 | CronService（dream/heartbeat/bound）、LocalTriggerStore、automation_turns（隐藏历史标记） |
| 31+ | FileStateStore / ExecSessionManager | 文件读/写去重追踪；shell 会话隔离 |
| — | WebUI / SDK / OS 服务 | 产品与部署层，本学习项目不追赶（记录备查） |

---

## 设计原则

1. **最小增量** — 每步只改最少的文件，独立可测试
2. **向后兼容** — AgentRunSpec、AgentLoop 接口只加可选字段
3. **可拆分** — 复杂功能跨步骤，步间可通过 fork + import 变更串联
4. **测试先行** — 每步增加相应测试，不破坏原有测试
