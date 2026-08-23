# Step117 接口契约（api-spec）

本文件定义 step117「子代理运行时限制同步（llm_timeout）」的对外契约。

## D1：SubagentManager.__init__ 新增可选参数

```python
def __init__(
    self, bus=None, provider=None, config=None, workspace="",
    restrict_to_workspace=None, max_concurrent_subagents=5, max_iterations=10,
    llm_wall_timeout_for_session: Callable[[str | None], float | None] | None = None,
) -> None: ...
```

契约：
- 新参数 `llm_wall_timeout_for_session` 接受 `Callable[[session_key: str | None], float | None]`；
- 缺省 `None` → 子代理 `llm_timeout_s` 回退为 `None`（env 默认 300s），与 step116 行为一致；
- 实例存为 `self._llm_wall_timeout_for_session`。

## D2：_run_subagent 写入 AgentRunSpec.llm_timeout_s

```python
sess_key = origin.get("session_key") if origin else None
llm_timeout = (
    self._llm_wall_timeout_for_session(sess_key)
    if self._llm_wall_timeout_for_session
    else None
)
AgentRunSpec(..., llm_timeout_s=llm_timeout)
```

契约：
- `spec.llm_timeout_s` 等于回调对 `origin["session_key"]` 的返回值；
- 回调返回 `0.0` → runner 禁用超时；`None` → env 默认；其余正数 → 该秒数超时；
- 无回调 → `spec.llm_timeout_s is None`。

## D3：main.py 接线

```python
subagent_manager = SubagentManager(
    ...,
    llm_wall_timeout_for_session=lambda sk: runner_wall_llm_timeout_s(session_manager, sk),
)
```

契约：`SubagentManager` 的 `llm_wall_timeout_for_session` 闭包以父 session_key 调用
`runner_wall_llm_timeout_s(session_manager, sk)`，返回父会话策略（sustained-goal → 0.0，否则 None）。

## D4：测试映射

| 契约 | 测试 |
| --- | --- |
| D1+D2 | 回调返回 `0.0`/`None` 时 `spec.llm_timeout_s` 对应；无回调时 `spec.llm_timeout_s is None` |
| D3 | （接线为单行注入，经 import 校验 + 子代理单测间接覆盖回调取值） |

> 全部测试使用 mock provider / 构造数据，禁止真实网络与 API 调用。
