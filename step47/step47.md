# Step 47 — `_request_finalization_retry` + 空响应处理

## 解决了什么问题及为什么

step46 的 runner 空响应处理有一个对齐缺口：空响应重试耗尽后直接用 fallback 文案，不发额外请求。

**当前行为（step46）**：
- `_MAX_EMPTY_RETRIES = 2`，空响应重试 2 次
- 重试耗尽后：直接用 `_EMPTY_FINAL_RESPONSE_MESSAGE` 作为最终内容
- 不发额外的无工具请求

**nanobot 行为**：
- 空响应重试耗尽后：调用 `_request_finalization_retry`，发一次无工具请求
- 无工具请求失败或仍为空：才用 fallback 文案
- 模型有机会基于已有对话和工具结果生成最终答案

### 最小增量范围

做 3 件事：
1. 添加 `_finalization_retry_messages` 静态方法
2. 添加 `_request_finalization_retry` 方法
3. 主循环空响应处理改为调用 `_request_finalization_retry`（try/except + content 检查 fallback）

不做：`_merge_message_content`、`_build_request_kwargs`、`_append_final_message` 等辅助方法提取（代码结构对齐，非功能）；finalization retry 的 usage 累积（留到 step49）。

## 目标和实现

### 目标

- 空响应重试耗尽后发一次无工具请求（finalization retry）
- 无工具请求失败或仍为空时 fallback 到 `_EMPTY_FINAL_RESPONSE_MESSAGE`
- 与 nanobot 行为一致，提升输出质量

### 实现

#### 1. `_finalization_retry_messages` 静态方法（runner.py）

```python
@staticmethod
def _finalization_retry_messages(messages):
    """构造 finalization 重试消息（无工具，让模型基于对话生成最终答案）。"""
    retry_messages = list(messages)
    retry_messages.append({
        "role": "user",
        "content": "Please provide your response to the user based on the conversation above.",
    })
    return retry_messages
```

#### 2. `_request_finalization_retry` 方法（runner.py）

```python
async def _request_finalization_retry(self, spec, messages, hook, iter_ctx):
    """空响应重试耗尽后发一次无工具请求。"""
    retry_messages = self._finalization_retry_messages(messages)
    return await self._request_no_tools(spec, retry_messages, hook, iter_ctx)
```

> 注：复用 step46 已有的 `_request_no_tools`（tools=None，经过 `_request_model`）。签名与 nanobot 不同（nanobot 是 `(spec, messages)`），最小增量保持现有签名。

#### 3. 主循环空响应处理（runner.py）

```python
if self._is_blank_text(clean):
    if empty_retries < _MAX_EMPTY_RETRIES:
        empty_retries += 1
        await hook.on_stream_end(iter_ctx, resuming=False)
        continue

    # step47：空响应重试耗尽后发 finalization retry（无工具请求）
    await hook.on_stream_end(iter_ctx, resuming=False)
    try:
        final_response = await self._request_finalization_retry(
            spec, messages, hook, iter_ctx,
        )
        final_content = final_response.content or _EMPTY_FINAL_RESPONSE_MESSAGE
    except Exception:
        final_content = _EMPTY_FINAL_RESPONSE_MESSAGE

    messages.append({"role": "assistant", "content": final_content})
    iter_ctx.final_content = final_content
    iter_ctx.stop_reason = "empty_final_response"
    # ... 后续排空注入逻辑不变 ...
```

**关键点**：
- `try/except` 包裹 finalization retry，失败时 fallback
- `final_response.content or _EMPTY_FINAL_RESPONSE_MESSAGE`：无工具请求仍为空时用 fallback
- 调用前 `on_stream_end` 关流，避免流悬挂

## 核心函数/类功能说明

### `_finalization_retry_messages`
静态方法，构造 finalization 重试消息。在现有消息后追加一条 user 提示，要求模型基于对话生成最终答案（无工具）。

### `_request_finalization_retry`
实例方法，空响应重试耗尽后发一次无工具请求。调用 `_finalization_retry_messages` 构造消息，然后调用 `_request_no_tools` 发送请求。

### 主循环空响应处理
空响应重试耗尽后不再直接用 fallback，而是发 finalization retry。try/except + content 检查确保异常或空响应时仍有 fallback。

## 暴露了什么问题

1. **`_request_no_tools` 经过 `_request_model`**：step46 的 `_request_no_tools` 调用 `_request_model(spec, messages, None, hook, iter_ctx)`，会经过 malformed_retry 逻辑。但 tools=None，provider 不应该返回 tool_calls，所以不会触发。风险低。
2. **流式回调**：`_request_no_tools` 经过 `_request_model`，会触发流式回调。调用前 `on_stream_end` 关流，避免上一段空流悬挂。
3. **usage 累积未做**：nanobot 在 finalization retry 后累积 usage。step47 最小增量不做，留到 step49（usage 升级）。
4. **辅助方法提取未做**：`_merge_message_content`、`_build_request_kwargs`、`_append_final_message` 等是代码结构对齐，非功能，留到后续。
5. **provider 调用次数**：finalization retry 是第 4 次 provider 调用（主循环 3 次 + retry 1 次），测试中需注意。

## 测试

新增 2 个测试类，5 个测试全部通过：

### TestStep47FinalizationRetryMessages（2 个）
| 测试 | 验证点 |
|------|--------|
| `test_finalization_retry_messages_contains_prompt` | 重试消息包含 user 提示文本 |
| `test_finalization_retry_messages_no_mutation` | 不修改原始 messages 列表 |

### TestStep47FinalizationRetryIntegration（3 个）
| 测试 | 验证点 |
|------|--------|
| `test_empty_response_finalization_retry_success` | 空响应耗尽后 finalization retry 返回正常内容 |
| `test_empty_response_finalization_retry_still_empty` | finalization retry 仍为空时用 fallback |
| `test_empty_response_finalization_retry_error` | finalization retry 异常时用 fallback |

全部测试：458 tests（453 原有 + 5 新增），3 个环境相关失败（与 step46 完全一致），**零回归**。

## 与 nanobot 对齐度

| 维度 | step46 | step47 后 |
|------|--------|----------|
| `_request_finalization_retry` | ❌ | ✅ |
| `_finalization_retry_messages` | ❌ | ✅ |
| 空响应耗尽后发无工具请求 | ❌ | ✅ |
| `_merge_message_content` | ❌ | ❌（后续） |
| `_build_request_kwargs` | ❌ | ❌（后续） |
| `_append_final_message` | ❌ | ❌（后续） |
| finalization retry usage 累积 | ❌ | ❌（step49） |

runner 对齐度：~68% → ~70%（A30 部分完成）。
agent 综合对齐度：~86% → ~87%。

## 下一 step 要解决什么

- **step48**：`hook.finalize_content` + reasoning 提取——不依赖 step47；
- **step49**：usage 估算升级——可补充 finalization retry 的 usage 累积；
- **后续**：`_merge_message_content`、`_build_request_kwargs`、`_append_final_message` 等辅助方法提取（代码结构对齐，非功能）。

step47 完成后，空响应处理与 nanobot 行为一致（多一次无工具请求），输出质量提升。
