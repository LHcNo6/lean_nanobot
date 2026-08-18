# Step 61：TurnContext 补齐 hooks/tools 字段

## 一、这一阶段解决什么问题以及为什么要这样做

### 问题背景

step60 的 `TurnContext` 作为一次 turn 的状态载体，已包含 `msg`/`session`/`runtime`/`initial_messages`/`ephemeral`/`trace` 等 30+ 字段，但**缺少四个 turn 级配置字段**：

| 缺失字段 | 类型 | 用途 |
|----------|------|------|
| `hooks` | `list[AgentHook]` | turn 级静态 hook 列表 |
| `hook_factories` | `list[AgentTurnHookFactory]` | turn 级 hook 工厂列表 |
| `turn_scopes` | `list[AbstractContextManager]` | turn 级 context manager 列表 |
| `tools` | `ToolRegistry \| None` | turn 级工具覆盖（None 用默认 registry） |

### 为什么这是技术债

1. **参数丢失**：`_run_agent_loop` 已支持 `hook_factories` 和 `turn_scopes` 参数，但 `_state_run` 调用时**没有传入**，导致 turn 级 hook 工厂和 scope 在正常 turn 路径中被静默忽略。

2. **状态不可传递**：`_run_agent_loop` 接收这些参数后只在局部使用，不存入 ctx。如果后续状态处理器（如 `_state_save`、`_state_respond`）需要访问 turn 级 hooks，无法从 ctx 获取。

3. **重复构建**：`_state_run` 在 `_run_agent_loop` 返回后，为了检查 `hook.wants_streaming()`，**重新调用 `_build_agent_spec`** 构建了一个全新的 spec（注释承认"ToolLoader.load 对已注册工具幂等"）。如果 ctx 中存有 hooks 等信息，至少可以保证两次构建的参数一致。

4. **扩展受阻**：`process_direct` 的 docstring 明确写着"hooks/hook_factories/tools 留到后续扩展"，因为 TurnContext 没有这些字段，direct 调用无法传递 turn 级配置。

### 为什么这样做（对齐 nanobot）

nanobot 的 `TurnContext` 包含这四个字段，其设计原则是：

- **ctx 是 turn 的单一真相源**：所有 turn 级配置存在 ctx 中，由入口（`_process_message` / `process_direct`）初始化
- **`_run_agent_loop` 保持无状态**：不接收 ctx，只接收原始参数，便于独立测试和复用
- **`_state_run` 是桥梁**：从 ctx 读取配置，传给 `_run_agent_loop`

这种设计使得 turn 级配置可以在状态机的各个状态间流动，而不需要每个方法都接收一长串参数。

---

## 二、原理思路和具体实现

### 2.1 字段设计

四个字段均使用 `field(default_factory=...)` 提供默认值，确保向后兼容：

```python
@dataclass
class TurnContext:
    # ... 已有字段 ...

    # step61：补齐 turn 级配置字段（对齐 nanobot）
    hooks: list[AgentHook] = field(default_factory=list)
    hook_factories: list[AgentTurnHookFactory] = field(default_factory=list)
    turn_scopes: list[AbstractContextManager[Any]] = field(default_factory=list)
    tools: ToolRegistry | None = None
```

- `hooks`/`hook_factories`/`turn_scopes`：空列表表示"无 turn 级配置"，使用全局默认
- `tools`：None 表示"使用默认 registry"，非 None 时覆盖

### 2.2 `_build_agent_spec` 扩展

新增 `hooks` 和 `tools` 参数（已有 `hook_factories`）：

```python
def _build_agent_spec(
    self,
    msg, session_key, session, initial_messages,
    *,
    # ... 已有参数 ...
    hooks: list[AgentHook] | None = None,          # step61 新增
    hook_factories: list[AgentTurnHookFactory] | None = None,
    tools: ToolRegistry | None = None,             # step61 新增
    # ...
) -> AgentRunSpec:
```

- `hooks` 传入 `AgentTurnHookSpec.turn_hooks`（turn 级静态 hook，在 registered_hooks 之后执行）
- `tools` 非 None 时覆盖 `AgentRunSpec.tools`（默认 `self.registry`）

### 2.3 `_run_agent_loop` 扩展

新增 `hooks` 和 `tools` 参数（已有 `hook_factories`/`turn_scopes`），透传给 `_build_agent_spec`：

```python
async def _run_agent_loop(
    self, initial_messages, *,
    msg, session, session_key, runtime,
    # ...
    hooks: list[AgentHook] | None = None,              # step61 新增
    hook_factories: list[AgentTurnHookFactory] | None = None,
    turn_scopes: list[AbstractContextManager[Any]] | None = None,
    tools: ToolRegistry | None = None,                 # step61 新增
    # ...
):
```

### 2.4 `_state_run` 透传

从 ctx 读取四个字段，传给 `_run_agent_loop`；重建 `_stream_spec` 时也传入，保证两次构建参数一致：

```python
async def _state_run(self, ctx: TurnContext) -> str:
    # ...
    final_content, tools_used, all_messages, stop_reason, had_injections = (
        await self._run_agent_loop(
            ctx.initial_messages,
            msg=ctx.msg,
            # ...
            hooks=ctx.hooks,                          # step61 新增
            hook_factories=ctx.hook_factories,        # step61 新增
            turn_scopes=ctx.turn_scopes,              # step61 新增
            tools=ctx.tools,                          # step61 新增
            # ...
        )
    )
    # ...
    # 重建 _stream_spec 时也传入 ctx 的 turn 级配置
    _stream_spec = self._build_agent_spec(
        ctx.msg, session_key, ctx.session, ctx.initial_messages,
        # ...
        hooks=ctx.hooks,                              # step61 新增
        hook_factories=ctx.hook_factories,            # step61 新增
        tools=ctx.tools,                              # step61 新增
        # ...
    )
```

### 2.5 `_process_system_message` 透传

系统通道目前不使用 turn 级配置，传入 `None`（保持现有行为），但参数链路打通。

---

## 三、该 step 的目标和实现

### 目标

1. TurnContext 新增 `hooks`/`hook_factories`/`turn_scopes`/`tools` 四个字段
2. `_build_agent_spec` 支持 `hooks` 和 `tools` 参数
3. `_run_agent_loop` 支持 `hooks` 和 `tools` 参数
4. `_state_run` 从 ctx 读取并透传所有四个字段
5. 现有测试全部通过（向后兼容）

### 不做的事（留到后续 step）

- **不消除 `_state_run` 重建 `_stream_spec` 的技术债**：这需要 `_run_agent_loop` 返回 hook 或接收 ctx，属于架构调整，留到 step63（`_build_agent_spec` 内联化）
- **不扩展 `process_direct` 支持 hooks/tools 参数**：字段已就绪，入口扩展留到后续 step
- **不修改 `_run_agent_loop` 返回值**：保持五元组，避免影响 `_process_system_message`
- **不修改 `_process_message` 初始化 ctx 时设置这些字段**：目前默认空列表/None 即可，后续入口扩展时设置

---

## 四、核心函数/类功能说明

### TurnContext（修改）

新增四个 turn 级配置字段：

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `hooks` | `list[AgentHook]` | `[]` | turn 级静态 hook，在全局 registered_hooks 之后执行 |
| `hook_factories` | `list[AgentTurnHookFactory]` | `[]` | turn 级 hook 工厂，在全局 registered_hook_factories 之后执行 |
| `turn_scopes` | `list[AbstractContextManager]` | `[]` | turn 级 context manager，运行期间进入，结束后退出 |
| `tools` | `ToolRegistry \| None` | `None` | turn 级工具覆盖；None 时使用默认 `self.registry` |

### `_build_agent_spec`（修改）

新增 `hooks` 和 `tools` 参数：
- `hooks` → `AgentTurnHookSpec.turn_hooks`
- `tools` → 非 None 时覆盖 `AgentRunSpec.tools`

### `_run_agent_loop`（修改）

新增 `hooks` 和 `tools` 参数，透传给 `_build_agent_spec`。

### `_state_run`（修改）

从 ctx 读取 `hooks`/`hook_factories`/`turn_scopes`/`tools`，传给 `_run_agent_loop` 和重建的 `_stream_spec`。

---

## 五、暴露了什么问题

1. **`_state_run` 重建 `_stream_spec` 仍未解决**：虽然现在两次构建的参数一致了，但仍然构建了两次 spec。根本原因是 `_run_agent_loop` 不返回 hook，而 `_state_run` 需要检查 `wants_streaming()`。需要在后续 step 中要么让 `_run_agent_loop` 返回 hook，要么将 `_build_agent_spec` 内联到 `_run_agent_loop` 并通过 ctx 传递。

2. **`_process_message` 未设置 ctx 的 turn 级字段**：目前所有正常 turn 的 `hooks`/`hook_factories`/`turn_scopes`/`tools` 都是默认值（空列表/None），字段虽然存在但未被入口使用。需要在后续扩展 `process_direct` 或 channel 级配置时设置。

3. **`tools` 字段的语义需要澄清**：nanobot 的 `ctx.tools` 是 turn 级工具覆盖，但 step60 的 `_build_agent_spec` 始终用 `self.registry`。step61 支持了覆盖，但默认行为不变。

---

## 六、下一步要解决什么

### step62：`_run_agent_loop` 签名扩展（channel/chat_id/message_id/metadata/original_user_text）

将 `_run_agent_loop` 的 `msg` 参数拆分为 `channel`/`chat_id`/`message_id`/`metadata`/`original_user_text` 等独立参数，对齐 nanobot 签名。这是 `process_direct` 完整支持和 harness 迁移的前置条件。

### step63：`_build_agent_spec` 内联化 + 消除 `_state_run` 重建 spec

将 `_build_agent_spec` 内联到 `_run_agent_loop`，并让 `_run_agent_loop` 通过 ctx 或返回值传递 hook 信息，彻底消除 `_state_run` 重建 spec 的技术债。

### step64：`run_dream` 迁移

从 loop.py 移除 `run_dream`，改为 harness 调用 `process_direct(ephemeral=True, tools=dream_tools)`。依赖 step62 的 `process_direct` 扩展。
