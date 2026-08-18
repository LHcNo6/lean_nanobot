# Step 41 — `ephemeral` 模式 + `run_extra_hooks_for_ephemeral`

## 解决了什么问题及为什么

step40 已在 `hook.py` 中预埋了完整的 ephemeral hook 逻辑（`AgentTurnHookSpec.ephemeral` 字段 + `build_agent_turn_hook` 条件分支），但 `loop.py` 的参数链完全未打通——`TurnContext` 没有 ephemeral 字段，`_process_message` / `_run_agent_loop` / `_build_agent_spec` 都不接受 ephemeral 参数，三个 state handler（`_state_build` / `_state_save` / `_state_respond`）也没有 ephemeral 条件分支。

这导致一类**临时 turn**（dream 后台记忆整理、heartbeat 活跃任务检测、一次性查询）无法复用现有状态机，只能走独立路径（如 step15 的 `_dream_loop`），造成代码重复。

nanobot 通过 `ephemeral` 标志位统一处理：在现有状态机中加条件分支，同一套代码处理正常 turn 和临时 turn。本 step 对齐 nanobot 设计，把 ephemeral 参数链从 `_process_message` 一路打通到 hook 构建和三个 state handler。

### 为什么选 ephemeral 标志位而不是独立路径

| 方案 | 说明 | 利弊 |
|------|------|------|
| 独立路径（如 `run_dream`） | 为临时 turn 写独立运行逻辑 | 代码重复，维护两套状态机 |
| **ephemeral 标志位（nanobot 方案）** | 现有状态机加条件分支 | 改动集中，无重复逻辑，为 `process_direct` 统一入口铺路 |

选择 ephemeral 标志位：最小增量，只加字段和条件分支，不改变状态机结构；为 step43 `process_direct(ephemeral=True)` 铺路。

### ephemeral 的精确定义

ephemeral **不是"完全不持久化"**，而是"**不做长期记忆维护**"：

| 操作 | 正常 turn | ephemeral turn |
|------|-----------|----------------|
| `_save_turn`（写入 session.messages） | 执行 | **仍执行**（当前 turn 消息需在会话上下文中可见） |
| `sessions.save`（持久化 session） | 执行 | **仍执行**（元数据需保存） |
| build 阶段 `consolidation` | 执行 | **跳过** |
| `enforce_file_cap`（文件容量裁剪） | 执行 | **跳过** |
| save 阶段后台 `consolidation` | 执行 | **跳过** |
| `include_memory_recent_history`（跨会话记忆） | 包含 | **不包含** |
| 完整 hook 链 | 执行 | **仅 progress hook**（除非 `run_extra_hooks_for_ephemeral=True`） |
| outbound.metadata `_stop_reason` | 不挂 | **挂载**（下划线前缀，供内部消费者） |

## 目标和实现

### 目标

1. `TurnContext` 新增 `ephemeral` / `run_extra_hooks_for_ephemeral` 字段；
2. `_process_message` 新增参数，传入 TurnContext；
3. `_run_agent_loop` 新增参数，传给 `_build_agent_spec`；
4. `_build_agent_spec` 新增参数，传给 `AgentTurnHookSpec`（hook.py 已支持）；
5. `_state_build`：ephemeral 时跳过 consolidation，`include_memory_recent_history=False`；
6. `_state_save`：ephemeral 时跳过 `enforce_file_cap` + 后台 consolidation；
7. `_state_respond`：ephemeral 时挂载内部 `_stop_reason`；
8. `_build_initial_messages` + `context.py` 新增 `include_memory_recent_history` 参数（接口对齐，no-op）。

### 实现

#### 1. `TurnContext` 新增字段（loop.py）

```python
@dataclass
class TurnContext:
    # ... 已有字段 ...
    # step41：ephemeral 临时 turn 模式
    ephemeral: bool = False
    run_extra_hooks_for_ephemeral: bool = False
```

#### 2. `_process_message` 新增参数

```python
async def _process_message(
    self, msg, session_key, *,
    pending_queue=None, runtime=None,
    ephemeral: bool = False,                          # step41 新增
    run_extra_hooks_for_ephemeral: bool = False,      # step41 新增
) -> OutboundMessage | None:
    ctx = TurnContext(
        # ...
        ephemeral=ephemeral,
        run_extra_hooks_for_ephemeral=run_extra_hooks_for_ephemeral,
    )
```

#### 3. `_run_agent_loop` 新增参数并透传

```python
async def _run_agent_loop(
    self, ...,
    ephemeral: bool = False,                          # step41 新增
    run_extra_hooks_for_ephemeral: bool = False,      # step41 新增
) -> tuple[...]:
    spec = self._build_agent_spec(
        ...,
        ephemeral=ephemeral,
        run_extra_hooks_for_ephemeral=run_extra_hooks_for_ephemeral,
    )
```

#### 4. `_build_agent_spec` 新增参数并传给 `AgentTurnHookSpec`

```python
def _build_agent_spec(
    self, ...,
    ephemeral: bool = False,                          # step41 新增
    run_extra_hooks_for_ephemeral: bool = False,      # step41 新增
) -> AgentRunSpec:
    hook = build_agent_turn_hook(AgentTurnHookSpec(
        ...,
        ephemeral=ephemeral,
        run_extra_hooks_for_ephemeral=run_extra_hooks_for_ephemeral,
    ))
```

> 注：`hook.py` 在 step40 已预埋 `AgentTurnHookSpec.ephemeral` / `run_extra_hooks_for_ephemeral` 字段和 `build_agent_turn_hook` 的条件分支（ephemeral 且不跑额外 hook 时只返回 progress hook）。step41 只需在 loop.py 中传递参数。

#### 5. `_state_run` 传递 ephemeral

`_state_run` 中调用 `_run_agent_loop` 和重建 `_stream_spec` 时都传入 `ctx.ephemeral` / `ctx.run_extra_hooks_for_ephemeral`，确保流式判断一致。

#### 6. `_state_build` 条件分支

```python
async def _state_build(self, ctx: TurnContext) -> str:
    replay_max_messages = replay_max_messages_for_context(runtime.context_window_tokens)
    # step41：ephemeral turn 跳过 build 阶段 consolidation
    if not ctx.ephemeral:
        await self.consolidator.maybe_consolidate_by_tokens(
            ctx.session, runtime=runtime, replay_max_messages=replay_max_messages,
        )
    # ...
    ctx.initial_messages = self._build_initial_messages(
        ...,
        include_memory_recent_history=not ctx.ephemeral,  # step41 新增
    )
```

#### 7. `_state_save` 条件分支

```python
async def _state_save(self, ctx: TurnContext) -> str:
    # ... _save_turn / clear_pending / clear_checkpoint 仍执行 ...
    # step41：ephemeral turn 跳过 enforce_file_cap + 后台 consolidation
    if not ctx.ephemeral:
        ctx.session.enforce_file_cap(...)
        self._schedule_background(
            self.consolidator.maybe_consolidate_by_tokens(...)
        )
    self.sessions.save(ctx.session)
```

> 关键：`_save_turn` 和 `sessions.save` 在 ephemeral 检查之前执行，只有 `enforce_file_cap` 和后台 consolidation 被跳过。

#### 8. `_state_respond` 挂载 `_stop_reason`

```python
async def _state_respond(self, ctx: TurnContext) -> str:
    # ... 构建 outbound ...
    # step41：ephemeral turn 挂载内部 _stop_reason（下划线前缀）
    if ctx.ephemeral and ctx.outbound is not None:
        ctx.outbound.metadata["_stop_reason"] = ctx.stop_reason
```

#### 9. `_build_initial_messages` + `context.py` 新增 `include_memory_recent_history`

- `_build_initial_messages` 新增 `include_memory_recent_history: bool = True` 参数，透传到 `context.build_messages`；
- `context.build_messages` 和 `context.build_system_prompt` 新增同名参数（透传）。
- step41 中 context.py 尚无 memory 集成，该参数为**接口对齐（no-op）**，等后续 memory 集成后填充实际逻辑。

## 核心函数/类功能说明

### `TurnContext.ephemeral: bool`
当前 turn 是否为临时运行模式。影响 `_state_build`（跳过 consolidation）、`_state_save`（跳过 enforce_file_cap + 后台 consolidation）、`_state_respond`（挂 `_stop_reason`）、hook 链（仅 progress hook）。

### `TurnContext.run_extra_hooks_for_ephemeral: bool`
ephemeral 模式下是否也执行完整 hook 链。`ephemeral=True and run_extra_hooks_for_ephemeral=False` → 仅 progress hook；其他组合 → 完整 hook 链。用于特定临时 turn 需要文件追踪等 hook 的场景。

### `_state_build` ephemeral 分支
- 跳过 consolidation：避免临时 turn 触发 token 预算压缩（consolidation 会调用 LLM，成本高）；
- `include_memory_recent_history=False`：临时 turn 不读取跨会话记忆，避免 dream 场景的记忆循环。

### `_state_save` ephemeral 分支
- 跳过 `enforce_file_cap`：临时 turn 不需要裁剪文件容量；
- 跳过后台 consolidation：临时 turn 不需要调度后台压缩任务；
- 仍执行 `_save_turn` + `sessions.save`：当前 turn 消息仍写入 session，元数据仍持久化。

### `_state_respond` 的 `_stop_reason`
下划线前缀与正常 `stop_reason` 区分，标识为内部字段。消费者（dream 处理器、heartbeat）根据 `_stop_reason` 判断停止原因以决定后续动作。

## 暴露了什么问题

1. **`include_memory_recent_history` 是 no-op**：step41 仅打通参数链，context.py 尚无 memory 集成，该参数不影响实际输出。等后续 step 集成 memory 后才能生效。
2. **`_state_run` 重建 `_stream_spec` 的技术债**：为判断流式输出，`_state_run` 末尾重新调用 `_build_agent_spec` 构建 `_stream_spec`。step41 必须给这个重建也传入 ephemeral，否则流式判断不一致。这个重建本身是技术债（align.md 风险提醒第 1 条），建议 step57 清理。
3. **`_save_turn` 在 ephemeral 时仍执行**：ephemeral turn 的消息会写入 session history。如果某些场景需要完全不持久化（如纯内存查询），需要后续 step 增加更细粒度的控制。
4. **`process_direct` 尚未实现**：ephemeral 参数链已打通，但还没有公共 API 入口。step43 将实现 `process_direct(prompt, session_key, ephemeral=True, ...)`，届时 dream 可从 `run_dream` 迁移到 `process_direct`。

## 测试

新增 `TestEphemeralMode` 测试类，18 个测试全部通过：

| 测试 | 验证点 |
|------|--------|
| `test_turn_context_default_ephemeral_false` | 默认值 False |
| `test_state_build_ephemeral_skips_consolidation` | ephemeral 时不调用 consolidation |
| `test_state_build_non_ephemeral_calls_consolidation` | 非 ephemeral 时调用 consolidation |
| `test_state_build_ephemeral_include_memory_false` | ephemeral 时 include_memory_recent_history=False |
| `test_state_build_non_ephemeral_include_memory_true` | 非 ephemeral 时 include_memory_recent_history=True |
| `test_state_save_ephemeral_skips_enforce_file_cap` | ephemeral 时不调用 enforce_file_cap |
| `test_state_save_non_ephemeral_calls_enforce_file_cap` | 非 ephemeral 时调用 enforce_file_cap |
| `test_state_save_ephemeral_skips_background_consolidation` | ephemeral 时不调度后台 consolidation |
| `test_state_save_non_ephemeral_schedules_background` | 非 ephemeral 时调度后台 consolidation |
| `test_state_save_ephemeral_still_saves_turn` | ephemeral 时仍调用 _save_turn |
| `test_state_respond_ephemeral_hang_stop_reason` | ephemeral 时挂载 _stop_reason |
| `test_state_respond_non_ephemeral_no_internal_stop_reason` | 非 ephemeral 时不挂 _stop_reason |
| `test_state_respond_ephemeral_suppress_response_no_stop_reason` | suppress_response 时 outbound=None |
| `test_process_message_passes_ephemeral_to_ctx` | _process_message 传递 ephemeral 到 ctx |
| `test_process_message_default_ephemeral_false` | 默认 ephemeral=False |
| `test_ephemeral_hook_only_progress` | ephemeral hook 链仅 progress hook |
| `test_ephemeral_run_extra_hooks_executes_full_chain` | run_extra_hooks_for_ephemeral=True 时完整 hook 链 |
| `test_build_initial_messages_passes_include_memory` | _build_initial_messages 传递 include_memory_recent_history |

全部测试：406 tests（388 原有 + 18 新增），3 个环境相关失败（与 step40 完全一致，openai/Python 版本差异），**零回归**。

## 与 nanobot 对齐度

| 维度 | step40 | step41 后 |
|------|--------|----------|
| TurnContext ephemeral 字段 | ❌ | ✅ |
| _process_message ephemeral 参数 | ❌ | ✅ |
| _run_agent_loop ephemeral 参数 | ❌ | ✅ |
| _build_agent_spec ephemeral 参数 | ❌ | ✅ |
| hook 链 ephemeral 逻辑 | ✅（预埋） | ✅（参数打通） |
| _state_build 跳过 consolidation | ❌ | ✅ |
| _state_save 跳过 enforce_file_cap | ❌ | ✅ |
| _state_save 跳过后台 consolidation | ❌ | ✅ |
| _state_respond 挂 _stop_reason | ❌ | ✅ |
| include_memory_recent_history 参数 | ❌ | ✅（接口对齐，no-op） |
| process_direct 公共 API | ❌ | ❌（step43） |

agent 综合对齐度：~78% → ~80%（A24 条目完成）。

## 下一 step 要解决什么

- **step42**：`_assemble_outbound` 提取 + MessageTool 抑制——从 `_state_respond` 提取出站消息组装为独立方法，meta 改用 `latency_ms` 替代 `tokens`，支持 `mt._sent_in_turn` 抑制；
- **step43**：`process_direct` 公共 API——依赖 step41 的 ephemeral 参数链，新增 `process_direct(prompt, session_key, ephemeral, hooks, hook_factories, tools, persist_user_message, runtime)`，绕过 bus 直接走状态机，`run_dream` 标记 deprecated；
- **step44**：StateTraceEntry 状态追踪——纯可观测性，不依赖 step41。

step41 完成后，ephemeral 基础设施就绪，step43 可以直接复用 `_process_message(ephemeral=True)` 实现 dream 迁移。
