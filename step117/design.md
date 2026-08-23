# Step117 架构设计：子代理运行时限制同步（llm_timeout）

## 1. 总体思路

复用 step116/主循环既有的 `runner_wall_llm_timeout_s`（sustained-goal → `0.0`，否则 `None`），
通过 nanobot 同款的「回调注入」把它接到子代理：

- `SubagentManager.__init__` 接收 `llm_wall_timeout_for_session` 回调（缺省 `None`）；
- `_run_subagent` 以 `origin["session_key"]`（父会话 key）调用回调，得到 `llm_timeout`；
- 写入 `AgentRunSpec.llm_timeout_s`，runner 据此做墙钟超时（与父策略一致）。

## 2. 改动点

### 2.1 SubagentManager.__init__（subagent.py）
```python
from collections.abc import Callable

def __init__(self, bus=None, provider=None, config=None, workspace="",
             restrict_to_workspace=None, max_concurrent_subagents=5,
             max_iterations=10,
             llm_wall_timeout_for_session: Callable[[str | None], float | None] | None = None):
    ...
    self._llm_wall_timeout_for_session = llm_wall_timeout_for_session
```

### 2.2 _run_subagent（subagent.py）
构造 `AgentRunSpec` 前：
```python
sess_key = origin.get("session_key") if origin else None
llm_timeout = (
    self._llm_wall_timeout_for_session(sess_key)
    if self._llm_wall_timeout_for_session
    else None
)
```
并在 `AgentRunSpec(...)` 增加 `llm_timeout_s=llm_timeout`。

> 语义：`0.0` 经 runner（`runner.py:394-395`）转为禁用超时；`None` 走 env 默认 300s。

### 2.3 main.py 接线
```python
from step117.goal_state import runner_wall_llm_timeout_s
subagent_manager = SubagentManager(
    bus=bus, provider=snapshot.provider, config=config, workspace=workspace,
    max_concurrent_subagents=defaults.max_concurrent_subagents,
    llm_wall_timeout_for_session=lambda sk: runner_wall_llm_timeout_s(session_manager, sk),
)
```
`session_manager` 已在 `main.py:94` 创建，回调闭包捕获它。

## 3. 数据流

```
父会话 spawn → origin["session_key"]=父key
   └─ SubagentManager._run_subagent
        └─ sess_key = origin["session_key"]
             └─ llm_timeout = llm_wall_timeout_for_session(sess_key)
                  └─ AgentRunSpec.llm_timeout_s = llm_timeout
                       └─ runner：0.0→禁用 / None→env默认 / 正数→超时
```

父策略来源：`runner_wall_llm_timeout_s(session_manager, 父key)` → 读父会话 metadata 是否 sustained-goal。

## 4. 利弊与风险

- 利：子代理墙钟超时与父会话对齐；完全复用既有策略函数，零重写。
- 风险/注意：
  - 回调返回 `0.0` 表示「禁用子代理超时」（仅 sustained-goal 父会话）；正常父会话返回 `None`，
    子代理仍受 env 默认 300s 约束——这是期望行为。
  - 未注入回调（测试 / 旧式构造）时 `llm_timeout_s=None` → 与 step116 行为一致，无回归。
  - 仅同步 `llm_timeout_s`；model/runtime 同步推迟（见 §6）。

## 5. 不在本 step 范围

- model/runtime 按父会话同步（learn_nano 子代理共享 `self._provider`，需改 `AgentRunSpec.runtime` 注入，
  属更大改动，留待后续）。
- `config.llm_timeout` 显式配置项（当前策略完全由会话 metadata 推导，无需新增配置）。

## 6. 下一 step（step118）

子代理 microcompaction 工具集对齐（核查 `governance.py` 是否覆盖 `list_exec_sessions`）。
