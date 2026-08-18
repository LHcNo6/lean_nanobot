# Step 49 — usage 估算升级

> 对齐 nanobot runner：provider 感知的链式估算替代简单 chars//4，usage 字典增加 total_tokens/estimated_tokens/provider_tokens。
> 上游：step48（hook.finalize_content + reasoning 提取）。
> 下游：step50（_run_tool hook 生命周期 + _execute_tools 三元组）。

## 一、本 step 做了什么

### 1.1 核心改动（7 个改动点，~80 行）

1. **新增 `_usage_dict(usage)` 静态方法**：转换 usage 字典为 int 值，过滤非数字值
2. **新增 `_usage_total(usage)` 静态方法**：计算总 token（优先 total_tokens，否则 prompt+completion）
3. **新增 `_merge_usage(left, right)` 静态方法**：合并两个 usage 字典（逐键相加）
4. **新增 `_estimate_response_usage(spec, messages, response)` 方法**：使用 `estimate_prompt_tokens_chain` + `estimate_message_tokens`，返回含 total_tokens/estimated_tokens 的 dict
5. **新增 `_usage_or_estimate(spec, messages, response)` 方法**：优先用真实 usage，缺失时估算，error 响应返回空 dict
6. **`_request_model` 中替换估算调用**：`_estimate_usage(messages, response)` → `_usage_or_estimate(spec, messages, response)`
7. **修改 `_accumulate_usage`**：累计所有键（包括 total_tokens/estimated_tokens），不再只累计 prompt/completion

### 1.2 移除

- 移除旧的 `_estimate_usage` 方法（简单 chars//4 估算）

### 1.3 新增导入

- `estimate_message_tokens`、`estimate_prompt_tokens_chain`（从 step49.helpers）

## 二、关键实现细节

### 2.1 保留现有架构（最小增量）

nanobot 在主循环中调用 `_usage_or_estimate`，不修改 response 对象。step49 保留 step48 的架构：usage 估算仍在 `_request_model` 内部处理（修改 response 对象），只升级估算算法。

### 2.2 `_usage_or_estimate` 逻辑

```python
def _usage_or_estimate(self, spec, messages, response):
    usage = self._usage_dict(response.usage)
    total = self._usage_total(usage)
    if total > 0:
        usage["total_tokens"] = total
        usage.setdefault("provider_tokens", total)
        return usage
    if response.finish_reason == "error":
        return {}
    return self._estimate_response_usage(spec, messages, response)
```

### 2.3 `_estimate_response_usage` 逻辑

```python
def _estimate_response_usage(self, spec, messages, response):
    tools = spec.tools.get_definitions()  # try/except 容错
    prompt_tokens, _ = estimate_prompt_tokens_chain(
        spec.provider, spec.model, messages, tools)
    completion_tokens = estimate_message_tokens({
        "role": "assistant", "content": response.content or ""})
    total_tokens = prompt_tokens + completion_tokens
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "estimated_tokens": total_tokens,  # 标记为估算值
    }
```

### 2.4 `spec.provider` vs `spec.runtime.provider`

nanobot 用 `spec.runtime.provider`，step49 的 `AgentRunSpec` 没有 `runtime` 属性，直接用 `spec.provider`。`estimate_prompt_tokens_chain` 接受 `None` 作为 provider（回退到通用估算），所以不会出错。

### 2.5 `build_assistant_message` 替代

nanobot 用 `build_assistant_message` 构造 assistant 消息，step49 没有这个函数。最小增量用简单 dict 替代：
```python
completion_tokens = estimate_message_tokens({
    "role": "assistant", "content": response.content or ""})
```

## 三、为什么是最小增量

| 做 | 不做（保留现有架构） |
|----|---------------------|
| 新增 `_usage_or_estimate` 方法 | 不将 usage 估算从 `_request_model` 移到主循环 |
| 新增 `_estimate_response_usage` 方法 | 不修改主循环的 `_accumulate_usage` 调用点 |
| 新增 `_usage_dict` / `_usage_total` / `_merge_usage` | 不将 `_accumulate_usage` 改为接受 dict（仍接受 response） |
| usage 字典增加 total_tokens/estimated_tokens/provider_tokens | 不改变 `_request_model` 的 response 重构逻辑 |
| `_accumulate_usage` 累计所有键 | |

## 四、测试

新增 10 个测试：

| 测试 | 验证点 |
|------|--------|
| `test_usage_dict_converts_values` | `_usage_dict` 正确转换 usage 字典，过滤非数字值 |
| `test_usage_dict_none` | `_usage_dict(None)` 返回空 dict |
| `test_usage_total_prefers_total_tokens` | `_usage_total` 优先用 total_tokens |
| `test_usage_total_falls_back_to_sum` | `_usage_total` 无 total_tokens 时用 prompt+completion |
| `test_merge_usage_combines_dicts` | `_merge_usage` 正确合并两个 usage dict |
| `test_estimate_response_usage_returns_total_and_estimated` | `_estimate_response_usage` 返回含 total_tokens/estimated_tokens |
| `test_usage_or_estimate_prefers_real` | `_usage_or_estimate` 优先用真实 usage，不估算 |
| `test_usage_or_estimate_falls_back_to_estimate` | `_usage_or_estimate` 缺失 usage 时估算 |
| `test_usage_or_estimate_error_returns_empty` | `_usage_or_estimate` 对 error 响应返回空 dict |
| `test_accumulate_usage_counts_all_keys` | `_accumulate_usage` 累计所有键（包括 total_tokens/estimated_tokens） |

测试结果：474 tests，3 环境相关失败（与 step48 完全一致），零回归。

## 五、对齐度

| 维度 | step48 | step49 后 |
|------|--------|----------|
| `_usage_or_estimate` 方法 | ❌ | ✅ |
| `_estimate_response_usage` 方法 | ❌（用 `_estimate_usage` chars//4） | ✅（链式估算） |
| `_usage_dict` / `_usage_total` / `_merge_usage` | ❌ | ✅ |
| usage 字典 total_tokens/estimated_tokens | ❌ | ✅ |
| `_accumulate_usage` 累计所有键 | ❌（只累计两个键） | ✅ |
| usage 估算在主循环中调用 | ❌（在 _request_model 中） | ❌（保留现有架构） |

runner 对齐度：~73% → ~75%（A32 部分完成）。
agent 综合对齐度：~88% → ~89%。

## 六、下一 step 衔接

- **step50**：`_run_tool` hook 生命周期 + `_execute_tools` 三元组——不依赖 step49；
- **step51**：SSRF/workspace 安全检测——不依赖 step49；
- **step52**：fail_on_tool_error + tool_events——不依赖 step49。

step49 完成后，usage 估算从简单 chars//4 升级为 provider 感知的链式估算，usage 字典更完整。
