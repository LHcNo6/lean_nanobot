# Step 17b — Content Recovery & Continuation Control

在 Step 17a (Governance & Tool Execution Safety) 基础上，聚焦 **AgentRunner 完成度保障**：处理 LLM 空白响应和 Token 耗尽续行，限制 Goal 续行和注入周期，防止无限循环。

---

## 目标

| 改进 | 说明 |
|------|------|
| 空内容重试 | LLM 返回空白时自动重试（最多 2 次），超限时发送 finalization message |
| Token 耗尽续行 | `finish_reason == "length"` 时自动续行（最多 3 次） |
| Goal 续行封顶 | session 级计数器限制最多 12 轮续行，而非无限 continue |
| 注入周期控制 | 每轮最多 5 个注入周期，每周期最多 3 条注入 |
| 注入消息合并 | 合并相邻 user 注入消息，避免 `[user, user]` 序列 |

---

## 改动文件

### 修改

| 文件 | 变更 |
|------|------|
| `runner.py` | 在 `_run_loop` 最终响应路径中增加重试/续行/封顶/合并逻辑 |

### 继承自 step17a（仅 `runner.py` 不同）

其余文件与 step17a 相同，import 路径 `step17a.` → `step17b.`

### 测试

| 文件 | 说明 |
|------|------|
| `test.py` | ~2275 行：原有 156 + step17a ~50 + step17b ~50 |

---

## 技术方案

### 1. 空内容重试

```python
# AgentRunSpec 新增字段
_MaxEmptyRetries: int = 2
_MaxLengthRecoveries: int = 3
_MaxInjectionCycles: int = 5
_MaxInjectionsPerTurn: int = 3
_MaxGoalContinuationRounds: int = 12
```

在最终响应路径中：

```python
# 空内容重试（before finalizing）
clean = response.content or ""
if is_blank_text(clean) and empty_retries < _MAX_EMPTY_RETRIES:
    empty_retries += 1
    await hook.on_stream_end(iter_ctx)
    continue  # 再次调用 LLM

# 超限后发送 finalization retry message
if is_blank_text(clean) and empty_retries >= _MAX_EMPTY_RETRIES:
    messages.append({"role": "user", "content": _build_finalization_retry_message()})
    response = await _request_model_no_tools(...)
    clean = response.content or ""
```

### 2. Token 耗尽续行

```python
if response.finish_reason == "length" and not is_blank_text(clean):
    if length_recovery_count < _MAX_LENGTH_RECOVERIES:
        messages.append(_build_assistant_message(response))
        messages.append({"role": "user", "content": _LENGTH_RECOVERY_PROMPT})
        length_recovery_count += 1
        continue  # 续行
```

```python
_LENGTH_RECOVERY_PROMPT = (
    "Please continue from where you left off. "
    "Your previous response was truncated."
)
```

### 3. Goal 续行封顶

与 step16 不同，step17b 的 goal 续行使用 **session 级别计数器**，而非无限 `continue`：

```python
# AgentRunSpec 新增
goal_continuation_rounds_key: str = "_goal_continuation_rounds"
```

在 goal_active_predicate 检查前读取 session metadata 中的续行计数：

```python
# 不在 runner 内无限续行
# runner 只负责当前 max_iterations 范围的续行
# session 级别的续行计数在 loop.py 的 _state_save 中管理

# runner 内简化：检查 goal_active 但记录已使用轮数
if spec.goal_active_predicate and spec.goal_active_predicate():
    rounds = spec.goal_continuation_rounds or 0
    if rounds >= _MAX_GOAL_CONTINUATION_ROUNDS:
        # 已达上限，正常结束
        return AgentRunResult(...)
    messages.append({
        "role": "user",
        "content": spec.goal_continue_message or "Continue working...",
    })
    continue
```

### 4. 注入周期控制

```python
# 在 tool_calls 路径和最终响应路径之间
injection_cycles = 0
had_injections = False

# ... tool execution ...
if spec.injection_callback:
    injected = await _drain_injections(spec, _MAX_INJECTIONS_PER_TURN)
    if injected and injection_cycles < _MAX_INJECTION_CYCLES:
        injection_cycles += 1
        had_injections = True
        # ...

# 最终响应路径
if spec.injection_callback and injection_cycles < _MAX_INJECTION_CYCLES:
    injected = await _drain_injections(spec, _MAX_INJECTIONS_PER_TURN)
    if injected:
        injection_cycles += 1
        had_injections = True
        continue  # 再次调用 LLM 处理注入
```

### 5. 注入消息合并

```python
def _append_injected_messages(self, messages, injected):
    """合并相邻 user 消息后追加。"""
    for msg in injected:
        if msg["role"] != "user":
            messages.append(msg)
            continue
        if messages and messages[-1]["role"] == "user":
            # 合并：追加到上一条 user 消息
            messages[-1]["content"] += "\n" + msg["content"]
        else:
            messages.append(msg)
```

---

## 新常量汇总

| 常量 | 值 | 用途 |
|------|-----|------|
| `_MAX_EMPTY_RETRIES` | 2 | 空白响应最多重试次数 |
| `_MAX_LENGTH_RECOVERIES` | 3 | Token 耗尽最多续行次数 |
| `_MAX_GOAL_CONTINUATION_ROUNDS` | 12 | Goal 续行最多轮数 |
| `_MAX_INJECTION_CYCLES` | 5 | 注入最多周期数 |
| `_MAX_INJECTIONS_PER_TURN` | 3 | 每周期最多注入条数 |

---

## 测试

| 测试类 | 测试内容 |
|--------|---------|
| 全部 step17a 测试 | 确保回归 |
| `TestEmptyContentRetry` | 空白响应重试 2 次、超限 fallback |
| `TestLengthRecovery` | finish_reason="length" 续行 3 次 |
| `TestGoalContinuationMaxRounds` | 12 轮封顶 |
| `TestInjectionCyclesLimit` | 5 周期封顶、3 条/周期 |
| `TestInjectionMerge` | 相邻 user 消息合并 |
