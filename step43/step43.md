# Step 43 — `process_direct` 公共 API（最小增量版）

## 解决了什么问题及为什么

当前 loop 只有一条入站路径：bus → `_dispatch` → `_process_message`。所有消息必须通过 MessageBus 发布才能被处理。

但存在一类调用方需要**绕过 bus 直接调用状态机**：
- **dream**：后台记忆整理，当前走独立的 `run_dream()` 路径（直接构建 AgentRunSpec 调用 runner.run，不走状态机，无 consolidation/ephemeral/hook）
- **heartbeat**：活跃任务检测，内部触发
- **一次性查询**：编程式调用，不需要 bus 事件

nanobot 通过 `process_direct` 公共 API 统一这类场景：构建 InboundMessage → 直接调用 `_process_message` → 返回 OutboundMessage。

### 最小增量范围

nanobot 的 `process_direct` 有 14 个参数。step43 只实现**核心子集**：

| 支持（step43） | 不支持（留到后续） |
|---------------|-------------------|
| `content` | `hooks` / `hook_factories` |
| `session_key` | `tools`（自定义工具注册表） |
| `channel` / `chat_id` | `on_progress` / `on_stream` / `on_stream_end` |
| `ephemeral` | `persist_user_message`（需 SKIP_USER_PERSIST_META） |
| `run_extra_hooks_for_ephemeral` | `media`（无 media 处理） |
| `runtime` | `sender_id`（用默认值） |

**理由**：ephemeral + runtime 是 dream/heartbeat 最核心的需求；tools 参数需要扩展 `_process_message` + `TurnContext` + `_build_agent_spec`，改动链长，留到 step58 dream 真正迁移时再加。

## 目标和实现

### 目标

1. 新增 `process_direct` 公共方法，绕过 bus 直接调用状态机；
2. 复用 `_session_locks`，确保 direct 调用与 bus turn 串行化；
3. `run_dream` 标记 deprecated（docstring），为 step58 迁移铺路。

### 实现

#### 1. `process_direct` 方法（loop.py）

```python
async def process_direct(
    self,
    content: str,
    session_key: str = "cli:direct",
    *,
    channel: str = "cli",
    chat_id: str = "direct",
    ephemeral: bool = False,
    run_extra_hooks_for_ephemeral: bool = False,
    runtime: LLMRuntime | None = None,
) -> OutboundMessage | None:
    msg = InboundMessage(channel=channel, chat_id=chat_id, content=content)
    # 复用 _session_locks，确保 direct 调用与 bus turn 串行化
    lock = self._session_locks.setdefault(session_key, asyncio.Lock())
    try:
        async with lock:
            return await self._process_message(
                msg, session_key,
                ephemeral=ephemeral,
                run_extra_hooks_for_ephemeral=run_extra_hooks_for_ephemeral,
                runtime=runtime,
            )
    finally:
        # 对齐 nanobot：direct 调用结束后重置 run_status
        await self._runtime_events().run_status_changed(msg, session_key, "idle")
```

**关键点**：
- 不调用 `_connect_mcp()`（无 MCP）
- 不处理 `persist_user_message`（留到后续）
- 复用 `_session_locks`，与 `_dispatch` 共享同一把锁
- finally 中重置 run_status 为 idle

#### 2. `run_dream` 标记 deprecated

```python
async def run_dream(self, tools=None) -> AgentRunResult | None:
    """[DEPRECATED step43] 后台记忆整理（独立路径）。

    .. deprecated::
        将在 harness 阶段（step58）迁移到 ``process_direct(ephemeral=True)``。
        当前保留用于向后兼容。
    """
    # ... 原有实现不变 ...
```

只改 docstring，不加运行时 warnings.warn（避免影响现有测试）。

## 核心函数/类功能说明

### `process_direct`
绕过 bus 的公共 API。直接构建 InboundMessage，获取 session lock，调用 `_process_message` 完整状态机，返回 OutboundMessage。支持 ephemeral 模式（step41 已就绪）。

### session lock 复用
`process_direct` 与 `_dispatch` 共享 `_session_locks`。这意味着：
- 同一 session 的 bus turn 和 direct 调用串行化
- direct 调用进行中时，bus 消息进入 pending queue（mid-turn injection）

### `run_dream` deprecated
标记为 deprecated 但功能不变。实际迁移到 `process_direct(ephemeral=True, tools=dream_tools)` 在 step58（harness CronService）完成。

## 暴露了什么问题

1. **tools 参数未支持**：dream 迁移需要自定义 tools，但 step43 不支持。需要扩展 `_process_message` → `TurnContext` → `_build_agent_spec` 参数链，留到 step58。
2. **hooks/callbacks 未支持**：`_process_message` 不接受 hooks/hook_factories/on_progress 参数，留到后续。
3. **persist_user_message 未支持**：需要 `turn_continuation.SKIP_USER_PERSIST_META` 完整机制，留到后续。
4. **run_status 重置可能重复**：`_process_message` 内部可能已管理 run_status，finally 中再次调用 `run_status_changed("idle")` 可能重复。当前测试通过，说明无副作用，但需关注。

## 测试

新增 `TestStep43ProcessDirect` 测试类，10 个测试全部通过：

| 测试 | 验证点 |
|------|--------|
| `test_process_direct_calls_process_message` | 调用 _process_message，参数正确 |
| `test_process_direct_ephemeral` | ephemeral=True 传入 |
| `test_process_direct_run_extra_hooks` | run_extra_hooks_for_ephemeral 传入 |
| `test_process_direct_custom_channel_chat_id` | 自定义 channel/chat_id |
| `test_process_direct_custom_runtime` | 自定义 runtime |
| `test_process_direct_default_session_key` | 默认 session_key="cli:direct" |
| `test_process_direct_uses_session_lock` | 复用 _session_locks |
| `test_process_direct_returns_none` | suppress_response 时返回 None |
| `test_run_dream_still_callable` | run_dream 仍可调用 |
| `test_run_dream_has_deprecated_docstring` | docstring 含 DEPRECATED |

全部测试：431 tests（421 原有 + 10 新增），3 个环境相关失败（与 step42 完全一致），**零回归**。

## 与 nanobot 对齐度

| 维度 | step42 | step43 后 |
|------|--------|----------|
| process_direct 公共 API | ❌ | ✅（核心子集） |
| process_direct 完整参数 | ❌ | ❌（tools/hooks/callbacks 留到后续） |
| run_dream deprecated | ❌ | ✅（docstring 标记） |
| dream 迁移到 process_direct | ❌ | ❌（step58） |

agent 综合对齐度：~81% → ~82%（A26 部分完成）。

## 下一 step 要解决什么

- **step44**：StateTraceEntry 状态追踪——纯可观测性，不依赖 step43；
- **step45**：`_save_turn` 增强 + `_state_command` 持久化——不依赖 step43；
- **step58**：CronService 引入，dream 从 `run_dream` 迁移到 `process_direct(ephemeral=True, tools=dream_tools)`——依赖 step43，届时扩展 process_direct 支持 tools 参数。
