# Step 40 — `turn_scopes` + `hook_factories`

## 解决了什么问题及为什么

step39 已有完整的 hook 装配基础设施（`AgentTurnHookFactory`、`AgentTurnHookSpec`、`build_agent_turn_hook`），但缺少两个关键接入点：

1. **`hook_factories`**：动态 hook 工厂。与静态 `hooks` 列表不同，工厂接收 `AgentTurnHookContext`，可根据 turn 上下文（channel、session_key、metadata 等）条件性创建 hook。nanobot 分两层：AgentLoop 级（`registered_hook_factories`，所有 turn 共享）和 turn 级（`turn_hook_factories`，仅该 turn）。
2. **`turn_scopes`**：turn 级 context manager。在 agent 运行期间进入，运行结束后退出，可用于临时修改环境变量、绑定资源、性能追踪等。

本 step 对齐 nanobot 设计，在 `AgentLoop.__init__`、`_build_agent_spec`、`_run_agent_loop` 中接入这两个机制。

## 目标和实现

### 目标
- `AgentLoop.__init__` 新增 `hook_factories` 参数（registered 级，所有 turn 共享）；
- `_build_agent_spec` 新增 `hook_factories` 参数（turn 级），传递给 `AgentTurnHookSpec`；
- `_run_agent_loop` 新增 `hook_factories` 和 `turn_scopes` 参数；
- `_run_agent_loop` 中用 `ExitStack` 进入 `turn_scopes`，`finally` 中关闭。

### 实现

#### 1. `AgentLoop.__init__` 新增 `hook_factories`

```python
def __init__(
    self, ...,
    hooks: list[AgentHook] | None = None,
    hook_factories: list[AgentTurnHookFactory] | None = None,  # step40 新增
    ...
):
    ...
    self.hooks = list(hooks) if hooks else []
    self._hook_factories = list(hook_factories) if hook_factories else []  # step40 新增
```

#### 2. `_build_agent_spec` 新增 `hook_factories` 参数

```python
def _build_agent_spec(
    self, ...,
    hook_factories: list[AgentTurnHookFactory] | None = None,  # step40 新增
) -> AgentRunSpec:
    ...
    hook = build_agent_turn_hook(AgentTurnHookSpec(
        ...
        registered_hook_factories=self._hook_factories,  # step40 新增
        turn_hook_factories=list(hook_factories or []),  # step40 新增
        registered_hooks=list(self.hooks),
    ))
```

#### 3. `_run_agent_loop` 新增参数 + ExitStack

```python
async def _run_agent_loop(
    self, ...,
    hook_factories: list[AgentTurnHookFactory] | None = None,  # step40 新增
    turn_scopes: list[AbstractContextManager[Any]] | None = None,  # step40 新增
) -> tuple[...]:
    ...
    self._sync_subagent_runtime_limits()
    turn_scope_stack = ExitStack()  # step40 新增
    try:  # step40 新增
        for scope in turn_scopes or ():
            turn_scope_stack.enter_context(scope)
        spec = self._build_agent_spec(
            ...,
            hook_factories=hook_factories,  # step40 新增
        )
        result = await self._runner.run(spec)
        ...
        return (...)
    finally:  # step40 新增
        turn_scope_stack.close()
```

## 设计决策

### 为什么分两层 hook 工厂？

对齐 nanobot：
- **registered（AgentLoop 级）**：`self._hook_factories`，所有 turn 共享，适合全局 hook（如文件编辑活动追踪）；
- **turn 级**：`_run_agent_loop` 参数，仅该 turn 有效，适合临时 hook。

装配顺序：`progress_hook → registered_hook_factories → registered_hooks → turn_hook_factories → turn_hooks`。

### 为什么 turn_scopes 在 `_run_agent_loop` 中管理？

turn_scopes 是运行时 context manager，生命周期与单次 agent run 绑定。用 `ExitStack` 统一管理，`finally` 中确保退出，与 nanobot 一致。

### 为什么不修改 `AgentRunSpec`？

step39 架构中 hook 已在 `_build_agent_spec` 中构建好（构建好的 `hook` 对象传给 `AgentRunSpec`），turn_scopes 在 `_run_agent_loop` 中管理，不需要传给 runner。nanobot 把 hook 构建放在 `_run_agent_loop` 中，所以 `AgentRunSpec` 有这些字段；step39 架构不同，不需要。

### 为什么不修改 `_state_run` 传递参数？

调用方暂不使用 `hook_factories`/`turn_scopes`，默认 None。后续需要时再从 `TurnContext` 传递。

## 核心函数/类功能说明

| 函数/类 | 位置 | 功能 |
|--------|------|------|
| `AgentLoop.__init__` 的 `hook_factories` | loop.py | AgentLoop 级 hook 工厂，存储为 `self._hook_factories` |
| `_build_agent_spec` 的 `hook_factories` | loop.py | turn 级 hook 工厂，传递给 `AgentTurnHookSpec` |
| `_run_agent_loop` 的 `turn_scopes` | loop.py | turn 级 context manager 列表，ExitStack 进入/退出 |
| `ExitStack` | contextlib | 统一管理多个 context manager 的进入/退出 |

## 测试

新增 `tests/test_turn_scopes_hook_factories.py`，12 个测试，2 个测试类：

| 测试类 | 测试数 | 覆盖点 |
|--------|--------|--------|
| `TestHookFactories` | 6 | 存储 / registered 应用 / turn 应用 / 返回 None 跳过 / 异常跳过 / 顺序 |
| `TestTurnScopes` | 6 | enter-exit / 多 scope 顺序 / None / 空列表 / 异常时仍退出 / 与 hook_factories 组合 |

全量测试：**429 passed**（step39: 417，新增 12），运行时间 12.01s。

## 暴露了什么问题

1. **`_state_run` 未传递 `hook_factories`/`turn_scopes`**：当前调用方不使用，默认 None。后续需要时需从 `TurnContext` 传递。
2. **`from_config` 未支持 `hook_factories`**：配置层暂不支持动态注册 hook 工厂。
3. **无具体 hook 工厂实现**：如 nanobot 的 `create_file_edit_activity_hook`，留待后续。
4. **`AgentRunSpec` 无 `hook_factories`/`turn_scopes` 字段**：与 nanobot 架构不同，step39 把 hook 构建放在 `_build_agent_spec` 中。

## 下一 step 要解决什么

- **step41**：`ephemeral` 模式 + `run_extra_hooks_for_ephemeral`（临时运行模式，只保留 progress hook）；
- **后续**：具体 hook 工厂实现（如文件编辑活动追踪）、`_state_run` 传递 `hook_factories`/`turn_scopes`、`from_config` 支持 hook 工厂注册。

## 与 nanobot 对齐度

| 维度 | step39 | step40 | nanobot |
|------|--------|--------|---------|
| `AgentTurnHookFactory` 类型 | ✅ | ✅ | ✅ |
| `AgentTurnHookSpec` 类 | ✅ | ✅ | ✅ |
| `build_agent_turn_hook` 函数 | ✅ | ✅ | ✅ |
| `AgentLoop.__init__` 的 `hook_factories` | ❌ | ✅ | ✅ |
| `_build_agent_spec` 传递 `registered_hook_factories` | ❌ | ✅ | ✅ |
| `_build_agent_spec` 传递 `turn_hook_factories` | ❌ | ✅ | ✅ |
| `_run_agent_loop` 的 `hook_factories` 参数 | ❌ | ✅ | ✅ |
| `_run_agent_loop` 的 `turn_scopes` 参数 | ❌ | ✅ | ✅ |
| `_run_agent_loop` 中 ExitStack 进入 turn_scopes | ❌ | ✅ | ✅ |
| `ephemeral` 模式 | ❌ | ❌（不做） | ✅ |
| `run_extra_hooks_for_ephemeral` | ❌ | ❌（不做） | ✅ |
| `AgentRunSpec` 的 `hook_factories`/`turn_scopes` | ❌ | ❌（不做） | ✅ |
| 具体 hook 工厂实现 | ❌ | ❌（不做） | ✅ |
