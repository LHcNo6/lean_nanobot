# Step117 需求定义：子代理运行时限制同步（llm_timeout）

## 1. 问题背景

step116 已完成子代理 system prompt 模板化。但子代理运行 `AgentRunSpec` 时**未传 `llm_timeout_s`**
（runner 字段 `runner.py:123` 已存在，默认 `None` → env `NANOBOT_LLM_TIMEOUT_S` 默认 300s）。
结果是：父会话若是 sustained-goal turn（`runner_wall_llm_timeout_s` 返回 `0.0` 禁用超时），
子代理仍受 300s 墙钟约束——父策略未同步到子代理，行为不对等。

## 2. 本 step 要解决什么

把父会话的墙钟超时策略同步到子代理：子代理的 `AgentRunSpec.llm_timeout_s` 由
`llm_wall_timeout_for_session(父session_key)` 决定（sustained-goal → `0.0`，否则 `None`），
对齐 nanobot `SubagentManager` 的 `llm_wall_timeout_for_session` 回调机制。

## 3. 为什么这样做（方案取舍）

- 直接复用 learn_nano 既有的 `runner_wall_llm_timeout_s(sessions, session_key)`（`goal_state.py:87`），
  与主线 `_build_agent_spec`（`loop.py:1337`）同源，保证父/子策略一致。
- 采用 nanobot 的「回调注入」形态：`SubagentManager.__init__` 接收
  `llm_wall_timeout_for_session` 回调（缺省 `None`），`_run_subagent` 调用它取 `llm_timeout`。
  回调在 `main.py` 接线为 `lambda sk: runner_wall_llm_timeout_s(session_manager, sk)`。
- 范围取舍：**只同步 `llm_timeout_s`**。路线图括号里的「（及 model/runtime）」推迟——
  learn_nano 子代理用共享 `self._provider`（`AgentRunSpec.provider`）而非 per-parent `runtime`，
  改 `runtime` 注入是更大改动，超出最小增量；本 step 不改 `config` schema（无 `llm_timeout` 配置项）。

## 4. 目标与实现边界（最小增量）

- 目标：子代理 `llm_timeout_s ==` 父会话策略；未接线时回退 `None`（env 默认），无回归。
- 边界（**不做**）：
  - 不改 `config` schema / 不加 `llm_timeout` 配置项；
  - 不同步 model/runtime（共享 provider，推迟）；
  - 不改 `AgentRunSpec` 其它字段语义。

## 5. 验收标准

1. `SubagentManager.__init__` 新增可选 `llm_wall_timeout_for_session` 回调，缺省 `None`。
2. `_run_subagent` 用该回调（以 `origin.session_key`）计算 `llm_timeout` 并写入 `AgentRunSpec.llm_timeout_s`。
3. `main.py` 构造 `SubagentManager` 时注入 `lambda sk: runner_wall_llm_timeout_s(session_manager, sk)`。
4. 测试：回调返回 `0.0`/`None` 时 spec 对应；无回调时 `spec.llm_timeout_s is None`。
5. 全量测试失败数与 step116 基线（25）持平，无新增回归。
