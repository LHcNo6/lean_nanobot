# Step 17a — Governance & Tool Execution Safety

在 Step 16 (Subagents + Sustained Goals) 基础上，聚焦 **AgentRunner 可靠性增强**：让 ContextGovernor 无条件保护每次 LLM 调用，引入并行工具执行、结果归一化、格式错误恢复和超时兜底。

---

## 目标

| 改进 | 说明 |
|------|------|
| ContextGovernor 默认集成 | 每次迭代无条件执行 8 阶段治理管线，无需 `governance_config` |
| 并发工具执行 | 按 `concurrency_safe` 分区，安全工具并行执行 |
| 工具结果归一化 | 自动处理空/超大/丢失的工具结果 |
| 格式错误工具调用 | 删除无效调用、全无效时重试、fallback 无工具请求 |
| LLM 超时 | `asyncio.wait_for` 兜底，防止模型挂死 |

---

## 改动文件

### 新增

| 文件 | 行数 | 说明 |
|------|------|------|
| `runner.py` | ~320 | 核心改动：新方法、常量、数据结构 |

### 继承自 step16（import 路径修改 `step16.` → `step17a.`）

`bus.py`, `consolidation.py`, `context.py`, `events.py`, `goal_state.py`, `governance.py`, `hook.py`, `llm.py`, `loop.py`, `main.py`, `memory.py`, `openai_compat_provider.py`, `provider.py`, `session.py`, `subagent.py`, `tool.py`, `tools/echo.py`, `tools/spawn.py`, `tools/long_task.py`

### 测试

| 文件 | 行数 | 说明 |
|------|------|------|
| `test.py` | ~2225 | 保留 156 个原有测试 + 新增 ~50 行并发/超时/归一化测试 |

---

## 技术方案

### 1. ContextGovernor 默认集成

```python
async def _run_loop(self, spec, messages, ...):
    # 无条件创建默认 governance config
    gov_config = spec.governance_config or ContextGovernanceConfig(
        tools=spec.tools,
        context_window_tokens=spec.context_window_tokens or 200_000,
        max_tokens=spec.max_tokens,
    )

    for iteration in range(spec.max_iterations):
        # 始终执行 governance pipeline（不再条件判断）
        messages = _GOVERNOR.prepare_for_model(
            gov_config, messages, compacted_tool_call_ids,
        )
        # ... rest of iteration ...
```

**`AgentRunSpec` 新增字段：**
- `concurrent_tools: bool = True`
- `llm_timeout_s: float | None = None`
- `context_window_tokens: int | None = None`

### 2. 并发工具执行

```python
def _partition_tool_batches(self, spec, tool_calls) -> list[list[tuple]]:
    """返回 [(tool_call, tool), ...] 的批次列表。
    同一批次的安全工具可并行执行。"""
    batches = []
    current_batch = []
    for tc in tool_calls:
        tool = spec.tools.get(tc.name)
        is_safe = tool is not None and tool.read_only and not tool.exclusive
        if not is_safe and current_batch:
            batches.append(current_batch)
            current_batch = []
        if is_safe:
            current_batch.append((tc, tool))
        else:
            batches.append([(tc, tool)])
    if current_batch:
        batches.append(current_batch)
    return batches

async def _execute_tool_batch(self, batch, spec, hook, iter_ctx):
    """执行一批工具。安全工具用 asyncio.gather，否则串行。"""
    if len(batch) > 1 and spec.concurrent_tools:
        coros = [self._run_tool(tc, tool, spec, hook, iter_ctx) for tc, tool in batch]
        return await asyncio.gather(*coros)
    results = []
    for tc, tool in batch:
        r = await self._run_tool(tc, tool, spec, hook, iter_ctx)
        results.append(r)
    return results
```

### 3. 工具结果归一化

```python
async def _run_tool(self, tc, tool, spec, hook, iter_ctx):
    """执行单个工具并归一化结果。"""
    result = await spec.tools.execute(tc.name, **tc.arguments)
    normalized = _GOVERNOR.normalize_tool_result(
        gov_config, tc.id, tc.name, result,
    )
    return normalized
```

### 4. 格式错误工具调用处理

```python
def _drop_malformed_tool_calls(self, tool_calls):
    """保留名称有效的工具调用。"""
    valid = [tc for tc in tool_calls 
             if isinstance(tc.name, str) and tc.name.strip()]
    return valid

# 在 _run_loop 中，当所有工具调用无效且 finish_reason="tool_calls"：
#   第一次：重试（malformed_retry=True）
#   第二次：fallback 无工具请求
```

### 5. LLM 超时

```python
async def _request_model(self, spec, messages, tools_defs, hook, iter_ctx):
    """封装 LLM 调用，外层 asyncio.wait_for 超时。"""
    timeout = spec.llm_timeout_s or 300.0
    wants_streaming = hook is not None and hook.wants_streaming()
    outer_timeout = max(300.0, timeout * 2) if wants_streaming else timeout

    async def _on_delta(text):
        iter_ctx.stream_content += text
        await hook.on_stream(iter_ctx, text)

    coro = spec.provider.chat_stream_with_retry(
        messages=messages, tools=tools_defs,
        model=spec.model, temperature=spec.temperature,
        max_tokens=spec.max_tokens, on_content_delta=_on_delta,
    )
    try:
        return await asyncio.wait_for(coro, timeout=outer_timeout)
    except asyncio.TimeoutError:
        return LLMResponse(content="", finish_reason="error",
                           usage={"prompt_tokens": 0, "completion_tokens": 0})
```

---

## 测试

| 测试类 | 测试内容 |
|--------|---------|
| 全部 156 个原有测试 | 确保回归 |
| `TestConcurrentToolExecution` | 安全工具并行、非安全工具串行 |
| `TestToolResultNormalization` | 空结果填充、超大结果截断 |
| `TestMalformedToolCallRecovery` | 无效名称丢弃、全无效重试 |
| `TestLLMTimeout` | 超时返回 error finish_reason |

---

## 不修改

- `loop.py` — `AgentRunSpec` 只加可选字段，完全向后兼容
- `tool.py` — 只需 `read_only` / `exclusive` 属性（加到 Tool 基类）
- `subagent.py` — 接口不变
- `session.py` — 独立模块，后续步骤
