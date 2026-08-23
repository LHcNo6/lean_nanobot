# Step117：子代理运行时限制同步（llm_timeout）

## 1. 问题背景

step116 完成子代理 system prompt 模板化。但子代理运行 `AgentRunSpec` 时**未传 `llm_timeout_s`**
（runner 字段 `runner.py:123` 已存在，默认 `None` → env `NANOBOT_LLM_TIMEOUT_S` 默认 300s）。
父会话若是 sustained-goal turn（`runner_wall_llm_timeout_s` 返回 `0.0` 禁用超时），子代理仍受 300s
墙钟约束——父策略未同步到子代理，行为不对等。

## 2. 这一 step 解决了什么 / 为什么这样做

把父会话的墙钟超时策略同步到子代理：子代理 `AgentRunSpec.llm_timeout_s` 由
`llm_wall_timeout_for_session(父session_key)` 决定（sustained-goal → `0.0`，否则 `None`），
对齐 nanobot `SubagentManager` 的 `llm_wall_timeout_for_session` 回调机制。

方案取舍：
- 直接复用 learn_nano 既有的 `runner_wall_llm_timeout_s(sessions, session_key)`（`goal_state.py:87`），
  与主线 `_build_agent_spec`（`loop.py:1337`）同源，保证父/子策略一致。
- 采用 nanobot「回调注入」形态：`SubagentManager.__init__` 接收 `llm_wall_timeout_for_session` 回调
  （缺省 `None`），`_run_subagent` 调用它取 `llm_timeout` 写入 `AgentRunSpec`。
- **只同步 `llm_timeout_s`**。路线图括号里的「（及 model/runtime）」推迟——learn_nano 子代理用共享
  `self._provider`（`AgentRunSpec.provider`）而非 per-parent `runtime`，改 `runtime` 注入是更大改动，
  超出最小增量；本 step 也不改 `config` schema（无 `llm_timeout` 配置项）。

## 3. 原理思路与具体实现

### 3.1 SubagentManager.__init__（subagent.py）
新增可选参数（缺省 `None`，向后兼容，无回归）：
```python
from collections.abc import Callable
def __init__(self, ..., llm_wall_timeout_for_session: Callable[[str | None], float | None] | None = None):
    ...
    self._llm_wall_timeout_for_session = llm_wall_timeout_for_session
```

### 3.2 _run_subagent（subagent.py）
构造 `AgentRunSpec` 前以 `origin["session_key"]` 解析并写入：
```python
sess_key = origin.get("session_key") if origin else None
llm_timeout = (
    self._llm_wall_timeout_for_session(sess_key)
    if self._llm_wall_timeout_for_session
    else None
)
AgentRunSpec(..., llm_timeout_s=llm_timeout)
```
> `0.0` 经 runner（`runner.py:394-395`）转为禁用超时；`None` 走 env 默认 300s。

### 3.3 main.py 接线（对齐 nanobot loop 注入）
```python
from step117.goal_state import runner_wall_llm_timeout_s
subagent_manager = SubagentManager(
    ...,
    llm_wall_timeout_for_session=lambda sk: runner_wall_llm_timeout_s(session_manager, sk),
)
```
`session_manager` 已在 `main.py:94` 创建，回调闭包捕获它。父策略来源：读父会话 metadata 是否 sustained-goal。

## 4. 核心函数 / 类功能说明

| 元素 | 职责 |
| --- | --- |
| `SubagentManager.__init__(llm_wall_timeout_for_session=...)` | 接收父会话墙钟超时解析回调（缺省 None） |
| `_run_subagent` 内 `llm_timeout` 计算 | 以父 session_key 取策略并写入 `AgentRunSpec.llm_timeout_s` |
| `main.py` 闭包 `lambda sk: runner_wall_llm_timeout_s(session_manager, sk)` | 把父策略接线到子代理 |

## 5. 暴露了什么问题 / 下一 step

- 暴露：仅同步 `llm_timeout_s`；model/runtime 仍共享父 `provider`，未逐父同步（推迟）。
- 暴露：`runner_wall_llm_timeout_s` 只区分「sustained-goal / 非」两态，learn_nano 无「自定义超时秒数」
  配置项——若未来需 per-session 自定义秒数，需要新增 config 字段并扩展该回调语义。
- 下一 step（step118）：子代理 microcompaction 工具集对齐（核查 `governance.py` 是否覆盖
  `list_exec_sessions`）。

## 6. 验证

- 新增 `tests/test_subagent_tool_isolation.py::TestSubagentRuntimeLimitSync`：3 个用例全绿。
  - sustained-goal 父会话 → `spec.llm_timeout_s == 0.0`；
  - 普通父会话 → `spec.llm_timeout_s is None`；
  - 未注入回调 → `spec.llm_timeout_s is None`（与 step116 一致，无回归）。
- 全量 `step117/tests`：**25 failed / 1158 passed**（与 step116 基线 25 持平，新增 3 通过，无新增回归）。
  失败用例为 Windows 既有问题，与子代理超时同步无关。
