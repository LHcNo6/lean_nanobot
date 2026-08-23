# step122 接口契约（api-spec）

> 本文件定义 step122「子代理 runtime（模型/生成参数）逐父同步」的对外契约。
> 改动范围：`subagent.py`（`_run_subagent` 内 AgentRunSpec 注入逻辑）。
> 复用既有通道：`origin["runtime"]`（step121 已透传）、`AgentRunSpec.model/temperature/max_tokens`。

## A. 输入契约：`origin["runtime"]`

| 键 | 类型 | 来源 | 缺省 |
| --- | --- | --- | --- |
| `runtime` | `LLMRuntime \| None` | `SpawnTool` 透传 `current_request_context().runtime` | `None` |

访问约定（防御式）：
- `model`：`runtime.model if (runtime and getattr(runtime, "model", "")) else None`
- `temperature`：`runtime.temperature if runtime else 0.7`
- `max_tokens`：`runtime.max_tokens if runtime else 4096`
- `provider`：`self._provider or (getattr(runtime, "provider", None) if runtime else None)`

## B. 输出契约：`AgentRunSpec` 四字段

`SubagentManager._run_subagent` 构造 `AgentRunSpec` 时，相关字段取值规则：

| 字段 | 取值规则 |
| --- | --- |
| `provider` | `self._provider`；若其为 `None` 则回退 `runtime.provider` |
| `model` | `origin["runtime"].model`（非空）否则 `None` |
| `temperature` | `origin["runtime"].temperature`（runtime 存在）否则 `0.7` |
| `max_tokens` | `origin["runtime"].max_tokens`（runtime 存在）否则 `4096` |

早于构建 spec 的守卫（保持不变的语义，仅扩展回退来源）：
```python
if provider is None:
    return
```

## C. 不变量

- `provider` 为 `None` 时 `_run_subagent` 直接返回（无运行）。
- 无 `runtime`（`origin` 缺 `runtime` 或值为 `None`）时退化为既有标量缺省，
  行为与 step121 完全一致（model=None、temp=0.7、max_tokens=4096）。
- `context_window_tokens`（governance）仍用 step120 的 `200_000`，**不在本契约同步**。
- 不新增 `AgentRunSpec.runtime` 字段；`runner.py` 行为不变。
- `req_ctx` 继续传 `runtime=origin.get("runtime")`（step120/121 既有行为不改）。

## D. 测试可见性（假 runner 捕获）

为便于断言，测试用假 `runner.run` 捕获传入的 `AgentRunSpec`：
```python
class _CaptureRunner:
    async def run(self, spec):
        self.captured = spec
        return AgentRunResult(final="ok")
```
断言 `captured.model` / `captured.temperature` / `captured.max_tokens` /
`captured.provider` 符合 §B 规则。
