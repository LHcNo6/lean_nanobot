# Step 11 — Hook 系统

## 目标

为 `AgentRunner.run()` 注入 **生命周期钩子**，使外部代码可以在 LLM 调用前后插入自定义逻辑（日志、监控、用量统计等），而不需要修改 runner 本身。

## 新增文件

| 文件 | 行数 | 职责 |
|------|------|------|
| `hook.py` | 95 | `AgentHook` 基类 + `AgentHookContext`/`AgentRunHookContext` + `CompositeHook` |

## 修改文件

| 文件 | 变化 |
|------|------|
| `runner.py` | `AgentRunSpec` 增加 `hook`/`session_key` 字段；`run()` 注入 hook 生命周期 |
| `loop.py` | 增加 `hooks` 参数，`_state_run` 传入 `AgentRunSpec.hook` |
| `test.py` | 增加 14 个 hook 测试，共 48 个测试 |

## Hook 生命周期

### 6 个钩子方法

```python
class AgentHook:
    async def before_run(self, ctx: AgentRunHookContext) -> None: ...
    async def after_run(self, ctx: AgentRunHookContext) -> None: ...
    async def on_error(self, ctx: AgentRunHookContext) -> None: ...
    async def on_finally(self, ctx: AgentRunHookContext) -> None: ...
    async def before_iteration(self, ctx: AgentHookContext) -> None: ...
    async def after_iteration(self, ctx: AgentHookContext) -> None: ...
```

所有方法默认为空操作，子类只需要覆盖需要的。

### 调用顺序

```
AgentRunner.run(spec):
  1. run_ctx = AgentRunHookContext(messages)
  2. hook.before_run(run_ctx)           ← 开始
  3. try:
  4.   for i in range(max_iterations):
  5.     iter_ctx = AgentHookContext(i, messages)
  6.     hook.before_iteration(iter_ctx) ← 每次迭代开始
  7.     LLM call (chat_with_retry)
  8.     accumulate usage
  9.     iter_ctx.response = response
  10.    if tool_calls:
  11.      execute tools
  12.      iter_ctx.tool_calls / tool_results
  13.    else:
  14.      iter_ctx.final_content = content
  15.    hook.after_iteration(iter_ctx)  ← 每次迭代结束
  16.    return or continue
  17. except BaseException as exc:
  18.    run_ctx.exception = exc
  19.    hook.on_error(run_ctx)           ← 异常时
  20.    raise
  21. else:
  22.    populate run_ctx from result
  23.    hook.after_run(run_ctx)          ← 成功时
  24.    return result
  25. finally:
  26.    hook.on_finally(run_ctx)         ← 始终执行
```

### Context 数据类

#### `AgentHookContext`（每次迭代）

| 字段 | 类型 | 说明 |
|------|------|------|
| `iteration` | `int` | 当前迭代序号（从 0 开始） |
| `messages` | `list[dict]` | 当前消息列表（实时引用） |
| `session_key` | `str \| None` | 会话标识 |
| `response` | `LLMResponse \| None` | LLM 返回的原始响应 |
| `usage` | `dict[str, int]` | 累计 token 用量 |
| `tool_calls` | `list[ToolCallRequest]` | 本次迭代的工具调用列表 |
| `tool_results` | `list[Any]` | 本次迭代的工具执行结果 |
| `final_content` | `str \| None` | 最终文本回复（仅非工具路径） |
| `stop_reason` | `str \| None` | 停止原因 |

#### `AgentRunHookContext`（整轮运行）

| 字段 | 类型 | 说明 |
|------|------|------|
| `messages` | `list[dict]` | 初始消息快照 |
| `final_content` | `str \| None` | 最终回复内容 |
| `tools_used` | `list[str]` | 使用的工具名称列表 |
| `usage` | `dict[str, int]` | 整轮 token 用量 |
| `stop_reason` | `str \| None` | 停止原因 |
| `error` | `str \| None` | 错误信息 |
| `exception` | `BaseException \| None` | 原始异常对象 |

## CompositeHook — 多 hook 组合

```python
class CompositeHook(AgentHook):
    def __init__(self, hooks: list[AgentHook]) -> None: ...
```

- 遍历所有子 hook，逐个调用对应方法
- **错误隔离**：单个 hook 的异常被 `logger.exception` 记录后继续执行其他 hook
- 适用于将多个独立 hook（日志、监控、统计）组合使用

## AgentLoop 集成

```python
loop = AgentLoop(
    ...
    hooks=[logger_hook, tracker_hook],  # 可选
)
```

`_state_run` 自动将 hooks 列表包装为 `CompositeHook` 传入 `AgentRunSpec.hook`。

## 测试

14 个新测试（48 个总计）：

| 测试 | 验证内容 |
|------|---------|
| `test_before_run_called` | `before_run` 被调用 |
| `test_after_run_called_on_success` | 成功后 `after_run` 被调用 |
| `test_on_error_called_on_exception` | 异常时 `on_error` 被调用 |
| `test_on_finally_always_called` | 无论成功/失败 `on_finally` 都被调用 |
| `test_before_iteration_called` | 每次迭代调用 `before_iteration` |
| `test_after_iteration_called` | 每次迭代调用 `after_iteration` |
| `test_iteration_context_state` | `AgentHookContext` 携带 response/usage |
| `test_run_context_state` | `AgentRunHookContext` 携带最终结果 |
| `test_composite_hook_fanout` | `CompositeHook` 遍历所有子 hook |
| `test_hook_error_isolation` | 一个 hook 失败不影响其他 |
| `test_custom_hook_usage_tracker` | 自定义 subclass 追踪累计用量 |
| `test_session_key_in_context` | `session_key` 正确传递 |
| `test_multiple_iterations_with_tools` | 多轮工具调用时 hook 工作正常 |
| `test_loop_with_hook` | AgentLoop 集成 hooks 参数 |

## 与 nanobot 对齐

```
nanobot/agent/hook.py → step11/hook.py (~70% 对齐)
  - 相同: AgentHook(ABC)、CompositeHook、AgentHookContext、AgentRunHookContext
  - 简化: 无 wants_streaming、finalize_content、per-tool 钩子
  - 路由: 使用 CompositeHook 的错误隔离模式

nanobot/agent/runner.py → step11/runner.py (~50% 对齐 hook 部分)
  - 相同: before_run/after_run + before_iteration/after_iteration + on_error + on_finally
  - 简化: 无 streaming delta、concurrent_tools、context_block_limit
```

## 下一站

Step 12 — 流式集成：`on_stream` / `on_stream_end` 钩子 + 流式 delta 发布到 MessageBus。
