# step34：`_persist_user_message_early` 提前持久化 + `_build_initial_messages` 提取

## 一、解决了什么问题及为什么

### 问题 1：用户消息持久化不含运行时上下文

step33 的 `_state_build` 中，用户消息持久化逻辑是内联的：

```python
ctx.session.add_message("user", ctx.msg.content)  # 只存原始文本，不含运行时上下文
```

而 nanobot 的 `_persist_user_message_early` 会将**运行时上下文 + marker** 一并持久化。

**影响**：step33 的历史回放中，上一轮用户消息不包含运行时上下文（如时间、工作目录等）。多轮对话时，LLM 看不到上一轮的运行时上下文，可能导致上下文丢失。

### 问题 2：`initial_messages` 构建逻辑重复

step33 的 `initial_messages` 构建在 `_state_build` 和 `_process_system_message` 中各有一份，逻辑重复且略有差异（goal_lines/identity/scope 计算重复）。nanobot 提取了独立的 `_build_initial_messages` 方法统一处理。

### 问题 3：持久化与构建顺序不对齐

| 步骤 | nanobot | step33 |
|------|---------|--------|
| 1 | `get_history` | `get_history` |
| 2 | `_build_initial_messages`（构建，含运行时上下文） | 持久化用户消息（不含运行时上下文） |
| 3 | `_persist_user_message_early`（持久化，含运行时上下文） | `build_messages`（构建，含运行时上下文） |

nanobot 先构建再持久化，step33 先持久化再构建。顺序差异本身不影响功能，但对齐后更清晰，且确保 `_build_initial_messages` 看到的历史不包含当前轮用户消息。

## 二、目标与实现

### 做什么（最小增量）

| 改动 | 说明 |
|------|------|
| 提取 `_build_initial_messages` 方法 | 统一 `_state_build` 和 `_process_system_message` 中的 initial_messages 构建逻辑 |
| 实现 `_persist_user_message_early` 方法 | 对齐 nanobot，持久化含运行时上下文 + marker 的用户消息 |
| 调整 `_state_build` 调用顺序 | 先 `_build_initial_messages`，再 `_persist_user_message_early` |
| 同步更新 `_process_system_message` | 使用 `_build_initial_messages`，subagent 消息不持久化 |

### 不做什么（边界）

| 不做 | 原因 |
|------|------|
| `context.build_messages` 参数扩展（media/channel/chat_id/sender_id 等） | 需要媒体处理基础设施，留待后续 |
| `agent_context.session_extra`（cli_app + mcp） | 需要 MCP 基础设施 |
| `automation_history_overrides`（cron/local trigger 文本覆盖） | 需要自动化触发基础设施 |
| `_meta` 嵌套结构转换 | step33 使用扁平 `RUNTIME_CONTEXT_HISTORY_META`，保持一致 |
| `_runtime_chat_id` / `_unified_session` | 需要统一会话基础设施 |
| `include_memory_recent_history` 参数 | 需要 memory 模块支持 |

## 三、核心函数/类功能说明

### 3.1 `_build_initial_messages`（loop.py:785）

**签名**：
```python
def _build_initial_messages(
    self,
    msg: InboundMessage,
    session: Session,
    history: list[dict[str, Any]],
    pending_summary: str | None,
    runtime_context_blocks: list[RuntimeContextBlock] | None = None,
    current_role: str = "user",
) -> list[dict[str, Any]]
```

**功能**：构建 LLM turn 的初始消息列表 `[system, *history, tail]`。

**实现要点**：
- 计算 `goal_state_runtime_lines` 并合并到 identity
- 计算 `workspace_scope`
- 调用 `self.context.build_messages`，参数与 step33 现有调用一致
- `current_role="assistant"` 时（subagent follow-up），`current_message=""`

**与 nanobot 的差异**：nanobot 传入 `media`、`channel`、`chat_id`、`sender_id`、`session_metadata`、`include_memory_recent_history`、`session_key`、`unified_session`，step34 暂不传入。

### 3.2 `_persist_user_message_early`（loop.py:832）

**签名**：
```python
def _persist_user_message_early(
    self,
    msg: InboundMessage,
    session: Session,
    runtime_context_blocks: list[RuntimeContextBlock] | None = None,
    **kwargs: Any,
) -> bool
```

**功能**：在 turn 开始前持久化触发用户消息（含运行时上下文 + marker）。

**实现步骤**：
1. 检查 `turn_continuation.should_persist_user_message(msg.metadata)`，不满足则返回 False
2. 检查 `has_text or runtime_context_blocks`，不满足则返回 False
3. 构建 `extra` dict（从 kwargs 初始化）
4. 调用 `append_runtime_context(text, runtime_context_blocks)` 获取合并文本和 marker
5. 若 marker 不为 None，存入 `extra[RUNTIME_CONTEXT_HISTORY_META]`
6. `session.add_message("user", text, **extra)`
7. `self._mark_pending_user_turn(session)`
8. `self.sessions.save(session)`
9. 返回 True

**与 nanobot 的差异**：nanobot 处理 `media_paths`、`agent_context.session_extra`、`automation_history_overrides`，step34 暂不处理，通过 `**kwargs` 预留扩展点。

### 3.3 `_state_build` 调整（loop.py:888）

**调整前（step33）**：
1. 持久化用户消息（不含运行时上下文）
2. 构建 initial_messages（含运行时上下文）

**调整后（step34）**：
1. 构建 initial_messages（含运行时上下文）
2. 持久化用户消息（含运行时上下文）

**关键顺序说明**：
- `get_history` 在持久化之前调用，所以当前轮的用户消息不会出现在历史中
- `_build_initial_messages` 使用 `ctx.history`（不含当前用户消息）+ `current_message` 构建
- `_persist_user_message_early` 在 `_build_initial_messages` 之后调用，持久化的消息只影响下一轮
- 不会出现运行时上下文重复

### 3.4 `_process_system_message` 同步更新（loop.py:1122）

- 使用 `_build_initial_messages` 替代内联的 `build_messages`
- subagent 消息（`is_subagent=True`）：`current_role="assistant"`，不调用 `_persist_user_message_early`
- 非 subagent 系统消息：`current_role="user"`，不调用 `_persist_user_message_early`（系统通道消息通常不需要持久化为用户输入）
- 保留 `scope` 计算（`_build_agent_spec` 需要）

## 四、暴露了什么问题

1. **`_build_initial_messages` 内部计算了 `scope` 但未返回**：`_process_system_message` 中 `_build_agent_spec` 需要 `workspace_scope=scope`，所以需要在外部重新计算一次 `scope`。开销可忽略，但代码略有重复。后续可考虑让 `_build_initial_messages` 返回 `(messages, scope)` 元组，或在 `TurnContext` 中缓存 `scope`。

2. **运行时上下文在历史中可见**：step34 持久化含运行时上下文的用户消息后，`get_public_history` 需要正确移除运行时上下文。step33 已实现 `get_public_history` → `get_history(include_runtime_context=False)` → `public_history_message` 链路，仍然有效。

3. **`_save_turn` skip 逻辑依赖 `user_persisted_early`**：`turn_continuation.adjust_initial_message_count` 会根据 `user_persisted_early` 调整 skip 数量。step34 的 `user_persisted_early` 由 `_persist_user_message_early` 返回，逻辑正确。

4. **测试 `test_state_build_attaches_blocks_in_memory_only` 行为变更**：step33 验证"运行时上下文只在内存中"，step34 改为验证"运行时上下文同时持久化"。测试已重命名为 `test_state_build_persists_blocks_in_history`。

5. **`**kwargs` 扩展点未使用**：`_persist_user_message_early` 的 `**kwargs` 预留了 media/mcp_presets 等扩展点，但当前未使用。后续实现 media/MCP 时会用到。

## 五、下一 step 要解决什么

### step35：`_run_agent_loop` 提取为独立方法

- nanobot 的 `_run_agent_loop` 是一个 217 行的独立方法，封装了 runner 运行、注入处理、错误恢复等逻辑
- step34 的 `_state_run` 中直接调用 `self._runner.run(spec)`，缺少 `_run_agent_loop` 层
- 高风险重构，需要仔细对齐注入回调、错误处理、streaming 等逻辑

### step36：`_drain_pending` 阻塞等待 subagent

- nanobot 在 `_state_run` 结束后调用 `_drain_pending`，阻塞等待 subagent 结果
- step34 尚无此机制，subagent 结果通过 `_process_system_message` 异步处理
- 需要实现 subagent 运行状态跟踪和阻塞等待

### step37：`context.build_messages` 参数扩展

- 扩展 `media`、`channel`、`chat_id`、`sender_id` 等参数
- 实现 `_build_user_content` 方法（base64 编码图片）
- 需要媒体处理基础设施

### 后续候选

- `_sanitize_assistant_replay_text` + media/cli_apps breadcrumb
- MCP Integration
- 真实通道（Telegram/Discord/Slack 等）
- cron/local trigger
- memory 文件系统/dream

## 测试结果

- **351 passed**（step33: 336，新增 15）
- 新增测试文件：`tests/test_persist_user_early.py`（15 个测试）
  - `TestPersistUserEarly`：9 个测试
  - `TestBuildInitialMessages`：6 个测试
- 修改测试：`tests/test_runtime_context.py::test_state_build_attaches_blocks_in_memory_only` → `test_state_build_persists_blocks_in_history`
