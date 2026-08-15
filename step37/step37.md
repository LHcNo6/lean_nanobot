# Step 37 — `llm_timeout_s` + `runner_wall_llm_timeout_s`

## 解决了什么问题及为什么

step36 的 runner 已有 `llm_timeout_s` 字段和 `asyncio.wait_for` 超时逻辑，但存在两个问题：

1. **`0.0` 禁用超时的 bug**：`timeout = spec.llm_timeout_s or 300.0` —— 当 `llm_timeout_s=0.0` 时，`0.0` 是 falsy，会被覆盖为 `300.0`，无法禁用超时。
2. **持续目标 turn 不应受超时限制**：nanobot 中持续目标（sustained-goal）turn 可能 legitimately 超过默认超时，通过 `runner_wall_llm_timeout_s` 返回 `0.0` 禁用超时。

本 step 对齐 nanobot 设计：
1. 新增 `runner_wall_llm_timeout_s` 函数：持续目标 turn 返回 `0.0`（禁用超时），普通 turn 返回 `None`（用默认超时）；
2. 修复 runner 超时逻辑：正确处理 `0.0` 禁用、添加 `NANOBOT_LLM_TIMEOUT_S` 环境变量、超时返回 `error_kind="timeout"`；
3. `_build_agent_spec` 中传递 `llm_timeout_s=runner_wall_llm_timeout_s(...)`。

## 目标和实现

### 目标
- 修复 `0.0` 禁用超时的 bug；
- 持续目标 turn 自动禁用 LLM 超时；
- 添加 `NANOBOT_LLM_TIMEOUT_S` 环境变量支持（默认 300 秒）；
- 超时响应携带 `error_kind="timeout"`。

### 实现

#### 1. `runner_wall_llm_timeout_s` 函数（goal_state.py:84-113）

```python
def runner_wall_llm_timeout_s(
    sessions: SessionManager,
    session_key: str | None,
    *,
    metadata: Mapping[str, Any] | None = None,
    message_metadata: Mapping[str, Any] | None = None,
) -> float | None:
    """持续目标 turn 返回 0.0（禁用超时），否则返回 None（用默认超时）。"""
    meta = metadata
    if meta is None and session_key:
        meta = sessions.get_or_create(session_key).metadata
    return 0.0 if sustained_goal_turn(meta, message_metadata=message_metadata) else None
```

- 持续目标 turn（`sustained_goal_turn` 为 True）→ 返回 `0.0`；
- 普通 turn → 返回 `None`（runner 使用默认超时）；
- 调用方已持有 metadata 时直接传入，避免重复查库。

#### 2. runner 超时逻辑修复（runner.py:264-310）

**修复前（step36）**：
```python
timeout = spec.llm_timeout_s or 300.0  # BUG: 0.0 or 300.0 = 300.0
outer_timeout = max(300.0, timeout * 2) if wants_streaming else timeout
response = await asyncio.wait_for(coro, timeout=outer_timeout)
```

**修复后（step37，对齐 nanobot）**：
```python
timeout_s: float | None = spec.llm_timeout_s
if timeout_s is None:
    raw = os.environ.get("NANOBOT_LLM_TIMEOUT_S", "300").strip()
    try:
        timeout_s = float(raw)
    except (TypeError, ValueError):
        timeout_s = 300.0
if timeout_s is not None and timeout_s <= 0:
    timeout_s = None  # 0.0 表示禁用超时

outer_timeout = (
    max(300.0, timeout_s * 2)
    if wants_streaming and timeout_s is not None
    else timeout_s
)

if outer_timeout is None:
    response = await coro  # 禁用超时
else:
    try:
        response = await asyncio.wait_for(coro, timeout=outer_timeout)
    except asyncio.TimeoutError:
        return LLMResponse(
            content=f"Error calling LLM: timed out after {outer_timeout:g}s",
            finish_reason="error",
            error_kind="timeout",  # step37 新增
            usage={"prompt_tokens": 0, "completion_tokens": 0},
        )
```

#### 3. `_build_agent_spec` 传递 `llm_timeout_s`（loop.py:1027-1032）

```python
return AgentRunSpec(
    ...
    llm_timeout_s=runner_wall_llm_timeout_s(
        self.sessions, session_key,
        metadata=session.metadata if session else None,
        message_metadata=msg.metadata,
    ),
    ...
)
```

## 核心函数/类功能说明

| 函数/字段 | 位置 | 功能 |
|----------|------|------|
| `runner_wall_llm_timeout_s()` | goal_state.py:84 | 持续目标 turn 返回 0.0，普通 turn 返回 None |
| `AgentRunSpec.llm_timeout_s` | runner.py:110 | LLM 请求墙钟超时（None=默认，0.0=禁用） |
| `NANOBOT_LLM_TIMEOUT_S` | 环境变量 | 默认超时秒数（默认 300） |
| `sustained_goal_turn()` | goal_state.py:49 | 判断是否为持续目标 turn |

## 测试

新增 `tests/test_llm_timeout.py`，12 个测试，3 个测试类：

| 测试类 | 测试数 | 覆盖点 |
|--------|--------|--------|
| `TestRunnerWallLlmTimeout` | 5 | 普通 turn / 持续目标 / 显式 /goal / metadata 优先 / None 回查 |
| `TestRunnerTimeoutLogic` | 4 | 0.0 禁用 / 环境变量 / 超时 error_kind / 默认超时不触发 |
| `TestBuildAgentSpecPassesTimeout` | 3 | 普通 turn / 持续目标 / 显式 /goal |

全量测试：**390 passed**（step36: 378，新增 12），运行时间 11.72s。

## 暴露了什么问题

1. **`0.0 or 300.0` bug**：step36 的 `timeout = spec.llm_timeout_s or 300.0` 无法正确处理 `0.0` 禁用超时，step37 已修复。
2. **缺少环境变量支持**：step36 硬编码默认 300 秒，step37 添加 `NANOBOT_LLM_TIMEOUT_S` 环境变量。
3. **超时响应缺少 `error_kind`**：step36 超时时返回 `content=""`，step37 添加 `error_kind="timeout"` 和超时描述。
4. **`NANOBOT_STREAM_IDLE_TIMEOUT_S`**：流式空闲超时是独立功能，nanobot 中用于流式 provider 的空闲检测，step37 不做。
5. **subagent `llm_timeout_s`**：subagent 路径的超时设置独立，step37 不做。

## 下一 step 要解决什么

- **step38**：配置层接入 `max_tool_iterations`（装配代码读取配置，默认值改为 200）；
- **step39**：`file_state` contextvar 绑定（文件状态上下文）；
- **step40**：`turn_scopes` + `hook_factories`（turn 级 context manager + hook 工厂）；
- **step41**：`ephemeral` 模式 + `run_extra_hooks_for_ephemeral`（临时运行模式）。

## 与 nanobot 对齐度

| 维度 | step36 | step37 | nanobot |
|------|--------|--------|---------|
| `AgentRunSpec.llm_timeout_s` 字段 | ✅ | ✅ | ✅ |
| runner 超时逻辑（0.0 禁用） | ❌（bug） | ✅ | ✅ |
| 环境变量 `NANOBOT_LLM_TIMEOUT_S` | ❌ | ✅ | ✅ |
| 超时 `error_kind="timeout"` | ❌ | ✅ | ✅ |
| `runner_wall_llm_timeout_s` 函数 | ❌ | ✅ | ✅ |
| `_build_agent_spec` 传递 `llm_timeout_s` | ❌ | ✅ | ✅ |
| 持续目标 turn 禁用超时 | ❌ | ✅ | ✅ |
| `NANOBOT_STREAM_IDLE_TIMEOUT_S` | ❌ | ❌（不做） | ✅ |
| subagent `llm_timeout_s` | ❌ | ❌（不做） | ✅ |
