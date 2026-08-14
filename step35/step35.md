# Step 35：`_run_agent_loop` 提取为独立方法 + max_iterations stream budget

## 解决了什么问题及为什么

### 问题

step34 中，agent 运行逻辑内联在两个地方：
- `_state_run`：主 turn 运行（构建 spec → runner.run → 结果处理）
- `_process_system_message`：系统通道运行（构建 spec → runner.run → save → respond）

两处逻辑高度重复，且与 nanobot 的架构不一致。nanobot 将运行逻辑提取为独立的 `_run_agent_loop` 方法，被 `_state_run` 和 `_process_system_message` 共用。

### 为什么

1. **消除重复**：`_build_agent_spec` + `runner.run` + 结果处理逻辑在两处重复，提取后统一维护。
2. **对齐 nanobot**：nanobot 的 `_run_agent_loop` 是 agent 运行的核心入口，提取后便于后续逐步对齐（contextvar 绑定、`_sync_subagent_runtime_limits`、`llm_timeout_s` 等）。
3. **max_iterations stream budget**：nanobot 在 `_run_agent_loop` 中处理 max_iterations 后的 stream 推送（如 Feishu 卡片更新），step34 缺少此功能。

## 目标和实现

### 目标

1. 提取 `_run_agent_loop` 方法，封装 `_build_agent_spec` + `runner.run` + max_iterations/error 处理。
2. `_state_run` 和 `_process_system_message` 改用 `_run_agent_loop`。
3. 实现 `should_stream_budget_response` 函数，支持 max_iterations 后的 stream 推送。
4. 保持现有行为不变（纯重构 + 增量功能）。

### 实现

#### 1. `session/turn_continuation.py`：新增 `should_stream_budget_response`

```python
def should_stream_budget_response(
    *,
    stop_reason: str,
    pending_queue_available: bool,
    session_metadata: Mapping[str, Any] | None = None,
    message_metadata: Mapping[str, Any] | None = None,
) -> bool:
```

- 仅在 `stop_reason == "max_iterations"` 时返回 True；
- 可续跑时（有 pending queue 且 goal 续跑可用）返回 False（由隐形续跑接管）；
- 依赖已有的 `should_finalize_on_max_iterations`。

#### 2. `loop.py`：新增 `_run_agent_loop` 方法

```python
async def _run_agent_loop(
    self,
    initial_messages: list[dict[str, Any]],
    *,
    msg: InboundMessage,
    session: Session | None,
    session_key: str,
    runtime: LLMRuntime,
    pending_queue: asyncio.Queue[InboundMessage] | None = None,
    request_context: RequestContext | None = None,
    on_progress: Callable[..., Awaitable[None]] | None = None,
    on_retry_wait: Callable[[str], Awaitable[None]] | None = None,
    on_stream: Callable[[str], Awaitable[None]] | None = None,
    on_stream_end: Callable[..., Awaitable[None]] | None = None,
) -> tuple[str | None, list[str], list[dict[str, Any]], str, bool]:
```

返回 `(final_content, tools_used, messages, stop_reason, had_injections)`，对齐 nanobot。

内部逻辑：
1. 调用 `_build_agent_spec` 构建 spec；
2. `result = await self._runner.run(spec)`；
3. 从 `spec.hook` 获取 effective stream 回调；
4. max_iterations 时，如 `should_stream_budget_response` 为 True，通过 stream 推送最终内容；
5. error 时记录日志；
6. 返回元组。

#### 3. `loop.py`：修改 `_state_run`

- 移除内联的 `_build_agent_spec` + `runner.run`；
- 调用 `_run_agent_loop` 获取返回元组；
- 重建 `ctx.result`（`_state_save` / `_state_respond` 依赖）；
- 从元组设置 `ctx.final_content` / `ctx.tools_used` / `ctx.all_messages` / `ctx.stop_reason` / `ctx.had_injections`；
- 重新构建临时 spec 获取 hook，设置 `ctx.on_stream`（对齐 step34 行为）；
- 保留 `maybe_continue_turn` 和 goal_continuation_rounds 同步。

#### 4. `loop.py`：修改 `_process_system_message`

- 移除内联的 `_build_agent_spec` + `runner.run`；
- 调用 `_run_agent_loop` 获取返回元组；
- 从元组解构 `final_content, _, all_messages, stop_reason, _`；
- 保留 `_save_turn`、清理、respond 逻辑；
- 移除未使用的 `scope` 变量（`_run_agent_loop` 内部计算）。

## 核心函数/类功能说明

### `should_stream_budget_response`

判断 max_iterations 边界是否应通过 stream 推送最终响应。

- **输入**：`stop_reason`、`pending_queue_available`、`session_metadata`、`message_metadata`；
- **输出**：bool；
- **用途**：`_run_agent_loop` 中 max_iterations 处理。

### `_run_agent_loop`

运行 agent 迭代循环的核心入口。

- **输入**：`initial_messages`、`msg`、`session`、`session_key`、`runtime`、`pending_queue`、`request_context`、各种回调；
- **输出**：`(final_content, tools_used, messages, stop_reason, had_injections)`；
- **用途**：`_state_run` 和 `_process_system_message` 共用。

## 暴露了什么问题

1. **`ctx.result` 依赖**：`_state_save` 和 `_state_respond` 依赖 `ctx.result`（`AgentRunResult` 对象），但 `_run_agent_loop` 返回元组。需要在 `_state_run` 中重建 `ctx.result`。
2. **`ctx.on_stream` 设置**：step34 在 `_state_run` 中从 `spec.hook` 设置 `ctx.on_stream`，但 `_run_agent_loop` 内部的 hook 是局部变量。需要在 `_state_run` 中重新构建临时 spec 获取 hook。
3. **`_build_agent_spec` 重复调用**：`_state_run` 中为了获取 hook 重新调用了一次 `_build_agent_spec`。`_build_agent_spec` 是轻量级的，`ToolLoader.load` 对已注册工具幂等，开销可忽略。
4. **`stop_reason` 值差异**：runner 正常完成时 `stop_reason` 是 "stop"（来自 `response.finish_reason`），不是 "completed"。测试断言需对齐。
5. **nanobot 参数未实现**：`_run_agent_loop` 简化签名缺少 `channel`、`chat_id`、`message_id`、`metadata`、`original_user_text`、`ephemeral`、`run_extra_hooks_for_ephemeral`、`hooks`、`hook_factories`、`turn_scopes`、`tools` 等参数，留待后续 step。

## 下一 step 要解决什么

### step36：`_sync_subagent_runtime_limits` + `self.max_iterations` 属性

- 新增 `self.max_iterations` 属性（替代硬编码的 5）；
- 实现 `_sync_subagent_runtime_limits` 方法，同步 subagent 的 max_iterations；
- `_run_agent_loop` 中调用 `_sync_subagent_runtime_limits`。

### step37：`llm_timeout_s` + `runner_wall_llm_timeout_s`

- 实现 `runner_wall_llm_timeout_s` 函数（持续目标 turn 返回 0.0 禁用超时）；
- `_run_agent_loop` 中传递 `llm_timeout_s` 给 `AgentRunSpec`。

### step38：`file_state` contextvar 绑定

- 实现 `file_state` 模块和 `bind_file_states` / `reset_file_states`；
- `_run_agent_loop` 中绑定 file_state contextvar（runner 外部）。

### step39：`turn_scopes` + `hook_factories`

- 实现 turn 级 context manager 支持；
- 实现 hook 工厂基础设施；
- `_run_agent_loop` 中支持 `turn_scopes` 和 `hook_factories` 参数。

## 测试结果

- **365 passed**（step34: 351，新增 14）
- 新增测试文件：`tests/test_run_agent_loop.py`（14 个测试）
  - `TestShouldStreamBudgetResponse`：5 个测试
  - `TestRunAgentLoop`：9 个测试

## 与 nanobot 对齐度

| 维度 | step34 | step35 | nanobot |
|------|--------|--------|---------|
| `_run_agent_loop` 提取 | ❌ 内联 | ✅ 提取 | ✅ |
| `_state_run` 调用 `_run_agent_loop` | ❌ | ✅ | ✅ |
| `_process_system_message` 调用 `_run_agent_loop` | ❌ | ✅ | ✅ |
| max_iterations stream budget | ❌ | ✅ | ✅ |
| `should_stream_budget_response` | ❌ | ✅ | ✅ |
| `_sync_subagent_runtime_limits` | ❌ | ❌（不做） | ✅ |
| contextvar 绑定（方法内） | ❌ | ❌（runner 已绑定） | ✅ |
| `file_state` contextvar | ❌ | ❌（不做） | ✅ |
| `llm_timeout_s` | ❌ | ❌（不做） | ✅ |
| `turn_scopes` | ❌ | ❌（不做） | ✅ |
| `hook_factories` | ❌ | ❌（不做） | ✅ |
| `ephemeral` 模式 | ❌ | ❌（不做） | ✅ |
| 返回元组 | ❌（ctx 字段） | ✅ | ✅ |
