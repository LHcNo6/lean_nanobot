# step62：`_run_agent_loop` 签名扩展——拆分 `msg` 为独立参数

## 1. 问题背景

step61 为 `TurnContext` 新增了 `hooks/hook_factories/turn_scopes/tools` 四字段，并在 `_run_agent_loop` 和 `_build_agent_spec` 中透传。但这两个方法的签名仍以 `msg: InboundMessage` 作为整体参数传入，与 nanobot 的签名存在差距。

nanobot 的 `_run_agent_loop` 签名将消息字段拆分为独立参数：
```python
async def _run_agent_loop(
    self,
    initial_messages,
    *,
    channel: str,
    chat_id: str,
    message_id: str | None,
    metadata: dict[str, Any] | None,
    original_user_text: str | None,
    session_key: str,
    session: Session | None,
    runtime: LLMRuntime,
    ...
)
```

## 2. 原理分析

### 2.1 为什么要拆分 `msg`？

1. **对齐 nanobot 签名**：nanobot 的 `_run_agent_loop` 不接收 `InboundMessage` 对象，而是接收拆分后的原始字段。这使得方法可以被不构造 `InboundMessage` 的调用方（如 harness 的 `process_direct`）直接调用。

2. **降低耦合**：`_run_agent_loop` 只需要 `msg` 的几个字段（`channel/chat_id/metadata/content`），不需要整个 `InboundMessage` 对象。拆分后方法的依赖更清晰。

3. **为后续内联化铺路**：step63 计划将 `_build_agent_spec` 内联到 `_run_agent_loop`，消除 `_state_run` 重建 `_stream_spec` 的技术债。拆分签名后，内联化更自然。

### 2.2 关键字参数 vs 位置参数

nanobot 的 `_run_agent_loop` 使用 `*` 分隔，所有消息字段都是 keyword-only。这有两个好处：
- 调用方必须显式指定参数名，避免参数顺序错误
- 新增参数时不会破坏现有调用

## 3. 实现方案

### 3.1 `_build_agent_spec` 签名变更

**变更前**：
```python
def _build_agent_spec(
    self,
    msg: InboundMessage,
    session_key: str,
    session: Session | None,
    initial_messages: list[dict[str, Any]],
    *,
    injection_callback=...,
    ...
)
```

**变更后**：
```python
def _build_agent_spec(
    self,
    channel: str,
    chat_id: str,
    session_key: str,
    session: Session | None,
    initial_messages: list[dict[str, Any]],
    *,
    message_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    original_user_text: str | None = None,
    injection_callback=...,
    ...
)
```

- `channel/chat_id` 作为位置参数（与 `session_key/session/initial_messages` 同级）
- `message_id/metadata/original_user_text` 作为 keyword-only 参数（带默认值，向后兼容）

### 3.2 `_run_agent_loop` 签名变更

**变更前**：
```python
async def _run_agent_loop(
    self,
    initial_messages,
    *,
    msg: InboundMessage,
    session: Session | None,
    session_key: str,
    runtime: LLMRuntime,
    ...
)
```

**变更后**：
```python
async def _run_agent_loop(
    self,
    initial_messages,
    *,
    channel: str = "cli",
    chat_id: str = "direct",
    message_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    original_user_text: str | None = None,
    session: Session | None = None,
    session_key: str | None = None,
    runtime: LLMRuntime,
    ...
)
```

所有参数都带默认值，保证向后兼容。

### 3.3 调用点更新

1. **`_run_agent_loop` 内部**：调用 `_build_agent_spec` 时传入拆分后的参数；`should_stream_budget_response(message_metadata=metadata)`。

2. **`_state_run`**：从 `ctx.msg` 提取字段传入 `_run_agent_loop`；重建 `_stream_spec` 时也传入拆分后的参数。

3. **`_process_system_message`**：从 `msg` 提取字段传入 `_run_agent_loop`。

## 4. 核心函数说明

### `_build_agent_spec(channel, chat_id, session_key, session, initial_messages, *, message_id, metadata, original_user_text, ...)`

构建 `AgentRunSpec`。`channel/chat_id` 用于 `workspace_scopes.for_turn()` 和流式回调；`metadata` 用于 hook 上下文和超时配置；`message_id` 用于 `AgentTurnHookSpec`；`original_user_text` 用于可观测性。

### `_run_agent_loop(initial_messages, *, channel, chat_id, message_id, metadata, original_user_text, session, session_key, runtime, ...)`

运行 agent 迭代循环。内部调用 `_build_agent_spec` 构建 spec，然后调用 `runner.run(spec)` 执行。拆分后的参数直接透传给 `_build_agent_spec`。

## 5. 暴露问题与下一步

### 5.1 暴露的技术债

1. **`_state_run` 仍重建 `_stream_spec`**：为了检查 `wants_streaming()`，`_state_run` 在 `_run_agent_loop` 返回后重新调用 `_build_agent_spec` 构建一个临时 spec。这是 step61 遗留的技术债，留到 step63 解决（将 `_build_agent_spec` 内联到 `_run_agent_loop`，返回值中包含 `wants_streaming` 信息）。

2. **`workspace_scope` 参数移除**：`_build_agent_spec` 不再接收 `workspace_scope` 参数，改为内部调用 `workspace_scopes.for_turn(channel, message_metadata)`。这简化了调用方，但意味着 `for_message` 方法的使用减少。

3. **测试文件批量更新**：由于签名变更，所有直接调用 `_build_agent_spec` 和 `_run_agent_loop` 的测试都需要更新参数。已通过脚本批量替换，部分边缘情况可能需要手动修复。

### 5.2 下一步规划

- **step63**：将 `_build_agent_spec` 内联到 `_run_agent_loop`，消除 `_state_run` 重建 `_stream_spec` 的技术债。`_run_agent_loop` 返回值中增加 `wants_streaming` 标志。
- **step64**：将 `run_dream` 从 `loop.py` 迁移到 harness（`main.py`），通过 `process_direct(ephemeral=True, tools=dream_tools)` 调用。
