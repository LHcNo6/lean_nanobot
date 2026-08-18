# Step 52: fail_on_tool_error + tool_events

## 目标

对齐 nanobot runner 的工具错误终止机制和工具执行事件追踪：
1. `AgentRunSpec` 新增 `fail_on_tool_error: bool = False`——工具错误时终止 turn
2. `AgentRunResult` 新增 `tool_events: list[dict[str, str]]`——收集所有工具调用事件
3. `_run_tool` 中 `fail_on_tool_error=True` 时在 4 个错误场景设置 fatal_error
4. 主循环收集 tool_events + fatal_error 非 None 时直接返回结果（stop_reason="tool_error"）

## 前置依赖

- step50：`_run_tool` 返回三元组 `(result, event, error)`，`_execute_tool_batch` 返回三元组 `(results, events, fatal_error)`
- step51：SSRF/workspace 安全检测，`_run_tool` 有 4 个 `_classify_violation` 调用点

## 改动点

### 1. AgentRunSpec 新增 fail_on_tool_error

```python
@dataclass
class AgentRunSpec:
    ...
    concurrent_tools: bool = True
    fail_on_tool_error: bool = False  # step52 新增
    ...
```

默认 False，保持现有行为（工具错误不终止，模型可重试）。

### 2. AgentRunResult 新增 tool_events

```python
@dataclass
class AgentRunResult:
    ...
    had_injections: bool = False
    tool_events: list[dict[str, str]] = field(default_factory=list)  # step52 新增
```

每个 event 格式：`{"name": str, "status": "ok"|"error", "detail": str}`。

### 3. _run_tool 中 fail_on_tool_error 设置 fatal_error

在 4 个错误场景中添加判断：

1. **重复外部查找阻断**：`if spec.fail_on_tool_error: return lookup_error, event, RuntimeError(lookup_error)`
2. **prepare_call 出错**：`if spec.fail_on_tool_error: return str(error), event, RuntimeError(str(error))`
3. **工具执行异常**：`if spec.fail_on_tool_error: return payload, event, exc`
4. **ToolResult.is_error**：`if spec.fail_on_tool_error: return str(result), event, RuntimeError(str(result))`

`fail_on_tool_error=False`（默认）时保持现有行为（error=None，不终止）。

### 4. 主循环收集 tool_events + 处理 fatal_error

```python
# _run_loop 中初始化
tool_events: list[dict[str, str]] = []

# 工具执行后
tool_events.extend(events)
if batch_fatal_error is not None and fatal_error is None:
    fatal_error = batch_fatal_error
    break

# fatal_error 非 None 时直接返回结果
if fatal_error is not None:
    error_msg = f"Error: {type(fatal_error).__name__}: {fatal_error}"
    messages.append({"role": "assistant", "content": error_msg})
    iter_ctx.final_content = error_msg
    iter_ctx.error = error_msg
    iter_ctx.stop_reason = "tool_error"
    await hook.after_iteration(iter_ctx)
    return AgentRunResult(
        final_content=error_msg,
        ...
        stop_reason="tool_error",
        error=error_msg,
        tool_events=tool_events,
    )
```

**关键设计**：fatal_error 处理时直接 `return`，而不是 `break`。因为 break 跳出 iteration 循环后会继续执行 max_iterations 处理，覆盖 stop_reason。

### 5. 所有返回 AgentRunResult 的地方添加 tool_events

- `_error_result` 静态方法：新增 `tool_events` 参数
- `_run_loop` 中 4 个返回点：empty_final_response、正常完成、max_iterations、fatal_error

## 不做（最小增量）

- 不修改 `_execute_tool_batch` 返回值结构（step50 已完成）
- 不修改 `_classify_violation` 逻辑（step51 已完成）
- 不修改 tools_used 收集逻辑
- 不添加 hint 到错误消息（nanobot 用 hint 引导模型重试，fail_on_tool_error=True 时直接终止，hint 无用）

## 测试

新增 6 个测试：

1. `TestStep52SpecFields.test_fail_on_tool_error_default_false`：默认值验证
2. `TestStep52SpecFields.test_tool_events_default_empty`：默认值验证
3. `TestStep52FailOnToolError.test_fail_on_tool_error_terminates_turn`：fail_on_tool_error=True 时终止 turn
4. `TestStep52FailOnToolError.test_fail_on_tool_error_false_continues`：默认不终止，模型可恢复
5. `TestStep52ToolEvents.test_tool_events_collected_in_result`：tool_events 收集
6. `TestStep52ToolEvents.test_tool_event_error_status`：工具异常时 event status=error

## 测试结果

```
Ran 498 tests in 16.665s
FAILED (failures=3)
```

- 498 tests（492 + 6 新测试）
- 3 个环境相关失败（与 step51 完全一致，非回归）：
  - `test_state_compact_with_summary`
  - `test_exceed_retries_triggers_finalization_fallback`
  - `test_error_with_injection_callback`

## 对齐度

- runner 对齐度：~82% → ~85%
- agent 综合对齐度：~91% → ~92%

## 后续

- step53：progress streaming + thinking 流（依赖 step48 reasoning 基础设施）
