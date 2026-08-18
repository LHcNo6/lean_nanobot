# Step 50 — _run_tool hook 生命周期 + 三元组返回

> 对齐 nanobot runner：工具执行标准 hook 生命周期 + 三元组返回 + CancelledError 分离。
> 上游：step49（usage 估算升级）。
> 下游：step51（SSRF/workspace 安全检测）。

## 一、本 step 做了什么

### 1.1 核心改动（4 个改动点，~80 行）

1. **`_run_tool` 改为返回三元组 + hook 生命周期 + CancelledError 分离**：
   - 返回 `(result, event, error)` 三元组
   - 添加 `before_execute_tool`/`after_execute_tool`/`on_execute_tool_error` hook 调用
   - `CancelledError` 单独 raise，不捕获
   - 其他异常捕获，调用 `on_execute_tool_error`，返回 error event

2. **`_execute_tool_batch` 改为返回三元组**：
   - 返回 `(results, events, fatal_error)` 三元组
   - 收集所有 `_run_tool` 的结果和事件
   - `fatal_error` 取第一个非 None 的 error

3. **主循环调用点解包三元组**：
   - `results, events, fatal_error = await self._execute_tool_batch(...)`
   - events/fatal_error 暂不使用（留到 step52 tool_events）

4. **`run()` 中 CancelledError 分离**：
   - `except asyncio.CancelledError`：不调 on_error，直接 raise
   - `except Exception`：调 on_error，raise

### 1.2 保留现有架构（最小增量）

- 保留 `_execute_tool_batch` 名称（不重命名为 `_execute_tools`）
- 保留现有参数（gov_config, tools_used）
- 不添加 `fail_on_tool_error` 逻辑（留到 step52）
- 不添加 SSRF/workspace 安全检测（留到 step51）

## 二、关键实现细节

### 2.1 `_run_tool` 处理顺序

```python
# 1. prepare_call（如有）
# 2. before_execute_tool hook
# 3. try 执行工具
#    - CancelledError → raise
#    - 其他异常 → on_execute_tool_error → 返回 (payload, error_event, None)
# 4. _emit_tool_progress
# 5. _GOVERNOR.normalize_tool_result
# 6. after_execute_tool hook
# 7. 构造 ok event
# 8. 返回 (normalized, ok_event, None)
```

### 2.2 event 结构

```python
# 成功
{"name": tool_name, "status": "ok", "detail": result_snippet[:120]}
# 失败
{"name": tool_name, "status": "error", "detail": error_message[:120]}
```

### 2.3 `fatal_error` 暂不设置

step50 中 `_run_tool` 异常时返回 `error=None`（不设 fatal_error），因为 `fail_on_tool_error` 逻辑留到 step52。`fatal_error` 字段已在三元组中预留，step52 会填充。

### 2.4 `run()` CancelledError 分离

```python
except asyncio.CancelledError as exc:
    run_ctx.exception = exc
    run_ctx.stop_reason = "cancelled"
    raise  # 不调 on_error
except Exception as exc:
    run_ctx.exception = exc
    await hook.on_error(run_ctx)
    raise
```

## 三、为什么是最小增量

| 做 | 不做（留到后续） |
|----|-----------------|
| `_run_tool` 返回三元组 | 不重命名 `_execute_tool_batch` 为 `_execute_tools` |
| `_run_tool` 添加 hook 生命周期调用 | 不添加 `fail_on_tool_error` 逻辑（step52） |
| `_execute_tool_batch` 返回三元组 | 不添加 SSRF/workspace 安全检测（step51） |
| `run()` CancelledError 分离 | 不添加 `external_lookup_counts` 参数（step51） |
| | 不将 events 存入 AgentRunResult（step52） |
| | 不添加 `is_tool_error_result` 检测（step52） |

## 四、测试

新增 7 个测试：

| 测试 | 验证点 |
|------|--------|
| `test_run_tool_returns_triple` | `_run_tool` 返回 (result, event, error) 三元组 |
| `test_run_tool_calls_before_execute_hook` | `_run_tool` 调用 before_execute_tool |
| `test_run_tool_calls_after_execute_hook` | `_run_tool` 成功后调用 after_execute_tool |
| `test_run_tool_calls_on_error_hook` | 工具异常时调用 on_execute_tool_error，返回 error event |
| `test_run_tool_cancelled_error_propagates` | CancelledError 不被捕获，向上传播 |
| `test_execute_tool_batch_returns_triple` | `_execute_tool_batch` 返回 (results, events, fatal_error) |
| `test_run_cancelled_error_no_on_error` | `run()` 中 CancelledError 不调 on_error |

测试结果：481 tests，3 环境相关失败（与 step49 完全一致），零回归。

## 五、对齐度

| 维度 | step49 | step50 后 |
|------|--------|----------|
| `_run_tool` hook 生命周期 | ❌ | ✅ |
| `_run_tool` 三元组返回 | ❌（只返回 result） | ✅ |
| `_execute_tool_batch` 三元组返回 | ❌（只返回 results） | ✅ |
| CancelledError 分离 | ❌ | ✅ |
| `fail_on_tool_error` | ❌ | ❌（step52） |
| SSRF/workspace 安全检测 | ❌ | ❌（step51） |
| tool_events 存入结果 | ❌ | ❌（step52） |

runner 对齐度：~75% → ~78%（A40 部分完成）。
agent 综合对齐度：~89% → ~90%。

## 六、下一 step 衔接

- **step51**：SSRF/workspace 安全检测——依赖 step50（`_run_tool` 三元组返回，在 _run_tool 中添加安全检测）；
- **step52**：fail_on_tool_error + tool_events——依赖 step50（三元组返回 + hook 生命周期，添加 fatal_error 处理和 events 存入结果）；
- **step53**：progress streaming + thinking 流——不依赖 step50。

step50 完成后，工具执行 hook 生命周期生效，三元组返回为 step51/52 奠定基础。
