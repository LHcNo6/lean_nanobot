# Step 46 — `_drop_malformed_tool_calls` 元组 + malformed_retry

## 解决了什么问题及为什么

step45 的 runner 有一个高风险对齐缺口：畸形 tool_call 处理可能导致 session 永久 wedge。

**当前行为（step45）**：
- `_drop_malformed_tool_calls(tool_calls: list) -> list`：返回过滤后的列表
- 主循环中 all_dropped 时：追加提示消息后 `continue`（**不重新请求模型**）
- 下一次迭代仍用原始 messages，可能重复触发畸形调用

**nanobot 行为**：
- `_drop_malformed_tool_calls(response) -> (dropped, all_dropped, original_finish_reason)`：返回元组，直接 mutate response
- `_request_model` 中 all_dropped 时：递归重试一次（`malformed_retry=True`）
- 重试仍失败时：降级为无工具请求

### 最小增量范围

做 5 件事：
1. 改造 `_drop_malformed_tool_calls` 返回元组 + mutate response
2. `_request_model` 添加 `malformed_retry` 递归逻辑
3. 添加 `_malformed_tool_call_retry_messages` 静态方法
4. 添加 `_request_malformed_fallback` 降级方法（直接调 provider）
5. 从主循环移除 malformed 处理（`_request_model` 已处理）

不做：`tc.has_valid_name()` 方法（用 hasattr 替代）、`_build_request_kwargs` 提取（step47）、progress streaming（step53）。

## 目标和实现

### 目标

- 畸形 tool_call 不再 wedge session
- all_dropped 时递归重试一次，仍失败降级无工具
- malformed 处理在 `_request_model` 内部，主循环不关心

### 实现

#### 1. `_drop_malformed_tool_calls` 改造（runner.py）

```python
@staticmethod
def _drop_malformed_tool_calls(response: LLMResponse) -> tuple[int, bool, str | None]:
    """返回 (dropped_count, all_dropped, original_finish_reason)。直接 mutate response。"""
    calls = getattr(response, "tool_calls", None)
    if not calls:
        return (0, False, getattr(response, "finish_reason", None))
    valid = [tc for tc in calls if hasattr(tc, 'name') and isinstance(tc.name, str) and tc.name.strip()]
    if len(valid) == len(calls):
        return (0, False, getattr(response, "finish_reason", None))
    dropped = len(calls) - len(valid)
    original_finish_reason = getattr(response, "finish_reason", None)
    response.tool_calls = valid
    if not valid:
        response.finish_reason = "stop"  # all_dropped 时改为 stop
    return (dropped, not valid, original_finish_reason)
```

#### 2. `_request_model` malformed_retry 逻辑（runner.py）

```python
async def _request_model(self, spec, messages, tools_defs, hook, iter_ctx, *, malformed_retry=False):
    # ... 现有请求、超时、usage 估算 ...

    dropped, all_dropped, original_finish_reason = self._drop_malformed_tool_calls(response)
    if all_dropped and original_finish_reason in ("tool_calls", "function_call") and not malformed_retry:
        retry_messages = self._malformed_tool_call_retry_messages(messages, response.content)
        return await self._request_model(spec, retry_messages, tools_defs, hook, iter_ctx, malformed_retry=True)
    if all_dropped and original_finish_reason in ("tool_calls", "function_call") and malformed_retry:
        fallback_messages = self._malformed_tool_call_retry_messages(messages, response.content)
        return await self._request_malformed_fallback(spec, fallback_messages)

    return response
```

#### 3. `_malformed_tool_call_retry_messages`（runner.py）

```python
@staticmethod
def _malformed_tool_call_retry_messages(messages, assistant_text):
    retry_messages = list(messages)
    note = "The previous model response attempted to call tools, but every tool call was malformed: ..."
    if assistant_text:
        note += f"\n\nPrevious assistant text before the malformed calls:\n{assistant_text}"
    retry_messages.append({"role": "user", "content": note})
    return retry_messages
```

#### 4. `_request_malformed_fallback`（runner.py）

```python
async def _request_malformed_fallback(self, spec, messages):
    """malformed_retry 仍失败时降级为无工具请求（直接调 provider，避免无限递归）。"""
    return await spec.provider.chat_with_retry(
        messages=messages, tools=None,
        model=spec.model, temperature=spec.temperature,
        max_tokens=spec.max_tokens,
    )
```

> 注：命名为 `_request_malformed_fallback` 而非 `_request_no_tools`，因为 step45 已有 `_request_no_tools`（经过 `_request_model`，会导致无限递归）。

#### 5. 主循环移除 malformed 处理（runner.py）

```python
# 移除前：
if response.tool_calls and response.finish_reason == "tool_calls":
    valid_calls = self._drop_malformed_tool_calls(response.tool_calls)
    if not valid_calls:
        messages.append({"role": "user", "content": _MALFORMED_TOOL_RETRY_MESSAGE})
        continue
    filtered = LLMResponse(content=response.content, tool_calls=valid_calls, ...)
    assistant_msg = self._build_assistant_message(filtered)
    ...

# 移除后：
if response.tool_calls and response.finish_reason == "tool_calls":
    # malformed 已在 _request_model 中处理，直接用 response
    assistant_msg = self._build_assistant_message(response)
    ...
```

## 核心函数/类功能说明

### `_drop_malformed_tool_calls`
静态方法，接收 LLMResponse，返回三元组 `(dropped_count, all_dropped, original_finish_reason)`。直接 mutate `response.tool_calls` 和 `response.finish_reason`。all_dropped 时 finish_reason 改为 "stop"，防止主循环误判。

### `_malformed_tool_call_retry_messages`
静态方法，构造 malformed 重试提示消息。包含通用提示文本和可选的原 assistant 文本（帮助模型理解上下文）。

### `_request_malformed_fallback`
实例方法，malformed_retry 仍失败时降级为无工具请求。直接调用 `provider.chat_with_retry(tools=None)`，不经过 `_request_model`（避免无限递归）。

### `_request_model` malformed_retry 参数
`malformed_retry: bool = False` 标记是否为重试请求。只在 `original_finish_reason in ("tool_calls", "function_call")` 时重试，排除正常 stop。递归只一次，仍失败降级。

## 暴露了什么问题

1. **`_request_no_tools` 命名冲突**：step45 已有 `_request_no_tools`（经过 `_request_model`，用于 max_iterations finalization）。malformed 降级不能复用它（会无限递归），所以新增 `_request_malformed_fallback`（直接调 provider）。后续可统一。
2. **重试消息不持久化**：malformed_retry 在 `_request_model` 内部递归，重试消息是局部变量，不进入主循环 messages，不持久化到 session。这是设计如此（malformed 是临时的）。
3. **`tc.has_valid_name()` 未实现**：用 `hasattr + isinstance + strip()` 替代，最小增量不添加新方法。
4. **现有测试更新**：`TestMalformedToolCallRecovery` 的两个测试更新——断言从 "invalid" 改为 provider 调用次数，`_AlwaysMalformedProvider` 添加 `chat_with_retry` 和 tools=None 分支。
5. **`_MALFORMED_TOOL_RETRY_MESSAGE` 常量保留**：主循环不再使用，但不删除（避免影响其他引用）。

## 测试

新增 3 个测试类，8 个测试全部通过：

### TestStep46DropMalformedToolCalls（4 个）
| 测试 | 验证点 |
|------|--------|
| `test_drop_malformed_returns_tuple` | 返回三元组，dropped/all_dropped/original 正确 |
| `test_drop_malformed_mutates_response` | tool_calls 被过滤，all_dropped 时 finish_reason 改 "stop" |
| `test_drop_malformed_no_calls` | 无 tool_calls 时返回 (0, False, finish_reason) |
| `test_drop_malformed_all_valid` | 全部有效时返回 (0, False, ...)，不修改 response |

### TestStep46MalformedRetryMessages（3 个）
| 测试 | 验证点 |
|------|--------|
| `test_retry_messages_contains_note` | 重试消息包含 malformed 提示文本 |
| `test_retry_messages_with_assistant_text` | assistant_text 包含在提示中 |
| `test_retry_messages_does_not_mutate_original` | 不修改原始 messages 列表 |

### TestStep46MalformedFallback（1 个）
| 测试 | 验证点 |
|------|--------|
| `test_fallback_calls_provider_without_tools` | 降级请求时 tools=None |

### 更新的现有测试
- `TestMalformedToolCallRecovery.test_invalid_name_dropped_and_retried`：断言从 "invalid" 改为 provider 调用次数=2
- `TestMalformedToolCallRecovery.test_all_invalid_twice_then_fallback`：`_AlwaysMalformedProvider` 添加 `chat_with_retry` 和 tools=None 分支

全部测试：453 tests（445 原有 + 8 新增），3 个环境相关失败（与 step45 完全一致），**零回归**。

## 与 nanobot 对齐度

| 维度 | step45 | step46 后 |
|------|--------|----------|
| `_drop_malformed_tool_calls` 元组返回 | ❌（返回 list） | ✅ |
| mutate response | ❌ | ✅ |
| `_request_model` malformed_retry | ❌ | ✅ |
| `_malformed_tool_call_retry_messages` | ❌ | ✅ |
| 降级无工具请求 | ❌ | ✅（`_request_malformed_fallback`） |
| 主循环 malformed 处理 | ✅（continue） | ❌（移到 _request_model） |
| `tc.has_valid_name()` 方法 | ❌ | ❌（用 hasattr 替代） |

runner 对齐度：~65% → ~68%（A29 完成）。
agent 综合对齐度：~85% → ~86%。

## 下一 step 要解决什么

- **step47**：`_request_finalization_retry` + 辅助方法提取——空响应耗尽后发独立无工具请求；`_merge_message_content`、`_build_request_kwargs`、`_append_final_message`/`_append_model_error_placeholder`。不依赖 step46，但 `_build_request_kwargs` 提取可复用 `_request_malformed_fallback`。
- **step48**：`hook.finalize_content` + reasoning 提取——不依赖 step46。

step46 是 runner 健壮性阶段的第一个 step，完成后 malformed tool_call 不再 wedge session。
