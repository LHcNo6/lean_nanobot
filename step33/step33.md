# step33：Session 历史回放增强 + Replay Overflow 压缩对齐

## 一、解决了什么问题及为什么

### 问题 1：历史回放窗口从 turn 中间截断

step32 的 `get_history` 使用简单的尾部切片 `unconsolidated[-max_messages:]`，可能从一个 turn 的中间开始（例如只包含 assistant 回复而缺少对应的 user 提问），导致 LLM 上下文不完整。

**nanobot 方案**：使用 `recent_message_start_index` 函数，支持 `extend_to_user` 参数，当窗口内没有 user 消息时向前扩展到最近的 user turn。

### 问题 2：历史回放包含内部元数据和命令消息

step32 的 `get_history` 直接返回原始消息，包含 `_command`、`_hidden_history`、`metadata` 等内部字段，可能污染 LLM 输入。

**nanobot 方案**：逐条处理消息，过滤 `_command` 消息、空 assistant 消息，只保留字段白名单（role / content / tool_calls / tool_call_id / name）。

### 问题 3：运行时上下文在历史回放中重复

step32 的 `get_public_history` 调用 `get_history` 后再逐条调用 `public_history_message`，但 `get_history` 已经返回了包含运行时上下文的消息，导致重复处理。

**nanobot 方案**：`get_history` 增加 `include_runtime_context` 参数，`get_public_history` 直接调用 `get_history(include_runtime_context=False)`。

### 问题 4：消息数超过回放窗口时没有提前压缩

step32 的 `maybe_consolidate_by_tokens` 只基于 token 预算压缩，当消息数超过回放窗口但 token 未超预算时，不会压缩，导致 `get_history` 丢弃大量消息而不归档。

**nanobot 方案**：新增 `_replay_overflow_boundary` 和 `_consolidate_replay_overflow`，在 token 预算压缩之前先归档超出回放窗口的消息。

### 问题 5：consolidation 调用位置不对齐

step32 在 `_state_compact` 中调用 `maybe_consolidate_by_tokens`，而 nanobot 在 `_state_build` 中调用（带 `replay_max_messages` 参数）。

**nanobot 方案**：`_state_compact` 只做 `auto_compact.prepare_session`，`_state_build` 中计算 `replay_max_messages` 并调用 `maybe_consolidate_by_tokens`。

---

## 二、目标与实现

| 模块 | 改动 | 对齐 nanobot |
|------|------|-------------|
| `helpers.py` | 新增 `recent_message_start_index` 函数 | ✅ `utils/helpers.py:350` |
| `session/manager.py` | 新增 `MIN_REPLAY_MAX_MESSAGES`、`REPLAY_TOKENS_PER_MESSAGE` 常量 | ✅ |
| `session/manager.py` | 新增 `replay_max_messages_for_context` 函数 | ✅ `session/manager.py:55` |
| `session/manager.py` | 重写 `get_history`：增加 `extend_to_user` / `include_runtime_context` 参数 | ✅ `session/manager.py:158` |
| `session/manager.py` | 更新 `get_public_history`：先过滤隐藏消息，再调用 `get_history(include_runtime_context=False)` | ✅ |
| `consolidation.py` | 新增 `_replay_overflow_boundary` 静态方法 | ✅ `memory.py:804` |
| `consolidation.py` | 新增 `_consolidate_replay_overflow` 方法 | ✅ `memory.py:840` |
| `consolidation.py` | 新增 `estimate_session_prompt_tokens` 方法 | ✅ `memory.py:877` |
| `consolidation.py` | `maybe_consolidate_by_tokens` 增加 `replay_max_messages` 参数，先做 replay overflow 压缩 | ✅ `memory.py:979` |
| `loop.py` | 新增 `_replay_token_budget` 静态方法 | ✅ `loop.py:752` |
| `loop.py` | `_state_compact` 去掉 consolidation 调用 | ✅ `loop.py:1550` |
| `loop.py` | `_state_build` 增加 consolidation 调用和新的 `get_history` 参数 | ✅ `loop.py:1594` |
| `loop.py` | `_process_system_message` 同步更新 consolidation 和 `get_history` 调用 | ✅ |

---

## 三、核心函数/类功能说明

### 3.1 `recent_message_start_index(messages, max_messages, *, extend_to_user=False)`

**位置**：`helpers.py`

返回最近回放窗口的起始索引。

- `max_messages <= 0` → 返回 `len(messages)`（空窗口）
- 不 `extend_to_user` 或消息数 <= max_messages → 返回尾部切片起点
- `extend_to_user` 且窗口内已有 user → 返回尾部切片起点
- `extend_to_user` 且窗口内无 user → 向前找最近的 user；若该 user 前一个是 `_channel_delivery`，则包含前一个
- 找不到 user → 返回尾部切片起点

### 3.2 `replay_max_messages_for_context(context_window_tokens)`

**位置**：`session/manager.py`

根据 context window 大小计算回放最大消息数。

公式：`min(FILE_MAX_MESSAGES, max(MIN_REPLAY_MAX_MESSAGES, context_window_tokens // REPLAY_TOKENS_PER_MESSAGE))`

- `MIN_REPLAY_MAX_MESSAGES = 120`
- `REPLAY_TOKENS_PER_MESSAGE = 100`
- None 或 <=0 返回 `FILE_MAX_MESSAGES`（2000）

### 3.3 `Session.get_history(max_messages, *, max_tokens, extend_to_user, include_runtime_context)`

**位置**：`session/manager.py`

返回未归档消息用于 LLM 输入。处理顺序：

1. 取未归档 tail（`self.messages[self.last_consolidated:]`）
2. 按 `max_messages` 切片：用 `recent_message_start_index`（支持 `extend_to_user`）
3. 避免从 turn 中间开始：从切片开头找第一个 user，若前一个是 `_channel_delivery` 则包含
4. `find_legal_message_start` 丢弃开头孤立的 tool 结果
5. 逐条处理：
   - 跳过 `_command` 消息
   - `include_runtime_context=False` 时调用 `public_history_message` 移除运行时上下文
   - 空 assistant 消息（无 tool_calls / reasoning_content / thinking_blocks）跳过
   - 只保留字段白名单：role / content / tool_calls / tool_call_id / name
6. `max_tokens` 预算：从尾部累加，超出预算则截断
7. token 预算后 user turn 对齐：找第一个 user 从 user 开始保留；若无 user，从原始 out 中恢复最近的 user

### 3.4 `Consolidator._replay_overflow_boundary(session, replay_max_messages)`

**位置**：`consolidation.py`（静态方法）

计算 replay overflow 压缩的结束索引。

- `replay_max_messages` 为空或 <=0 → 返回 None
- 未归档 tail 长度 <= replay_max_messages → 返回 None
- 用 `recent_message_start_index(tail, replay_max_messages, extend_to_user=True)` 找起始
- 从起始找第一个 user；若前一个是 `_channel_delivery` 则包含
- `find_legal_message_start` 找合法起始
- 返回第一个可见消息的绝对索引；若 <= last_consolidated 则返回 None

### 3.5 `Consolidator._consolidate_replay_overflow(session, replay_max_messages, *, runtime)`

**位置**：`consolidation.py`

归档会被回放消息窗口隐藏的消息。

1. 调用 `_replay_overflow_boundary` 找结束索引
2. 取 chunk = `session.messages[last_consolidated:end_idx]`
3. 调用 `archive(chunk, runtime=runtime, session_key=session.key)`
4. 更新 `session.last_consolidated = end_idx`，保存 session

### 3.6 `Consolidator.estimate_session_prompt_tokens(session, *, runtime)`

**位置**：`consolidation.py`

估算完整未归档 session tail 的 prompt token 数。

1. 取未归档完整历史
2. 构建 probe_messages（含历史、当前消息占位、session_summary、session_metadata）
3. 调用 `estimate_prompt_tokens_chain(runtime.provider, runtime.model, probe_messages, tool_definitions)`
4. 返回 `(token_count, source)` 元组

### 3.7 `AgentLoop._replay_token_budget(runtime)`

**位置**：`loop.py`（静态方法）

从 context window 推导 session 历史回放的 token 预算。

公式：`context_window_tokens - max(1, max_output) - 1024`，若结果 <= 0 则返回 `max(128, context_window_tokens // 2)`。

---

## 四、暴露了什么问题

### 问题 1：`get_public_history` 必须先过滤隐藏消息

由于 `get_history` 的字段白名单会移除 `_hidden_history` 元数据，`get_public_history` 必须在调用 `get_history` **之前**先过滤隐藏消息，否则无法识别。step33 通过创建临时 Session 解决了这个问题，但增加了一次对象拷贝。

### 问题 2：`_state_build` 中 `ctx.runtime` 可能为 None

测试中发现 `TurnContext` 的 `runtime` 属性可能为 None（例如直接调用 `_state_build` 而不经过完整状态机）。step33 通过 `runtime = ctx.runtime or self.runtime` 回退解决。

### 问题 3：`estimate_session_prompt_tokens` 依赖 `_build_messages`

`estimate_session_prompt_tokens` 调用 `self._build_messages` 构建 probe_messages，但 `_build_messages` 的参数签名可能与实际 `ContextBuilder.build_messages` 不一致。step33 使用了兼容的参数列表，但未来可能需要调整。

### 问题 4：media / cli_apps breadcrumb 未实现

nanobot 的 `get_history` 在 user 消息有 media 时合成 `[image: path]` 占位，有 cli_apps 时合成 CLI App Attachment 占位。step33 未实现这些，因为需要媒体处理基础设施。

### 问题 5：`_sanitize_assistant_replay_text` 未实现

nanobot 的 `get_history` 对 assistant 文本调用 `_sanitize_assistant_replay_text` 清理内部标记。step33 未实现，留待后续研究。

---

## 五、下一 step 要解决什么

### step34：`_persist_user_message_early` 提前持久化 + `_build_initial_messages` 提取

- 实现 `_persist_user_message_early`：turn 开始前持久化含运行时上下文 + marker 的用户消息到 session
- 提取 `_build_initial_messages` 为独立方法，统一构建 initial_messages
- `get_history` 回放前调用 `public_history_message` 避免运行时上下文重复追加

### step35：`_run_agent_loop` 提取为独立方法

- 纯重构：把核心循环从 `_process_message` 提取为 `_run_agent_loop`（217行）
- 高风险：涉及核心循环，需充分回归测试

### step36：`_drain_pending` 阻塞等待 subagent

- subagent 仍在运行时阻塞等待 pending_queue，保持 runner 循环存活
- 需要 subagent 运行状态跟踪

### 后续候选

- media / cli_apps breadcrumb
- `_sanitize_assistant_replay_text`
- Hook `finalize_content` 调用
- `tool_events` / `fail_on_tool_error`
- MCP Integration
- 真实通道（telegram / discord / slack 等）

---

## 测试结果

- 总测试数：**336 passed**（step32 有 297 个，新增 39 个）
- 新增测试文件：
  - `tests/test_session_history.py`（27 个测试）
  - `tests/test_consolidation_replay.py`（14 个测试）
- 所有测试使用 mock 或构造数据，无真实 API 调用
