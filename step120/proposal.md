# step120 需求定义：子代理运行配置传播

## 1. 问题背景

step110–119 已完成子代理核心九维度对齐。step119 调研发现，子代理 `AgentRunSpec`
虽已传播 `llm_timeout_s`、RequestContext、workspace_scope，但**运行期行为配置**仍未对齐
nanobot：

- 父配置 `agents.defaults.max_tool_result_chars`（默认 16_000）未被子代理 `AgentRunSpec`
  采用，子代理沿用 runner 默认 `ContextGovernanceConfig()`（缺省同样 16_000，但 `context_window_tokens`
  等预算维度与 nanobot 不一致）；
- 父配置 `agents.defaults.fail_on_tool_error`（默认 `True`）未被传播，子代理沿用
  `AgentRunSpec` 默认值 `False`，工具错误语义与父代理不一致；
- `finalize_on_max_iterations` 子代理沿用 `True`（生成收尾 fallback），而 nanobot 子代理
  硬编码 `False`（max-iterations 边界由隐形续跑接管）；
- `max_iterations_message` 沿用 learn_nano 自有 fallback 文案，与 nanobot 子代理文案不一致。

上述缺口对应路线图 §6 的 **G1/G2/G3/G4**，是「增强/打磨」层中唯一影响子代理**实际运行行为
正确性**的缺口（优先级最高）。

## 2. 目标

对齐 nanobot 子代理运行配置传播：把 `config` 中的运行限制，经 `SubagentManager.__init__`
提取后，注入每次 `_run_subagent` 构造的 `AgentRunSpec`，使子代理运行行为受父配置约束。

## 3. 需求定义（最小增量）

- **E1 — 工具结果截断阈值传播**：`agents.defaults.max_tool_result_chars` →
  `AgentRunSpec.governance_config.max_tool_result_chars`。
- **E2 — 工具错误升级策略传播**：`agents.defaults.fail_on_tool_error` →
  `AgentRunSpec.fail_on_tool_error`。
- **E3 — max-iterations 语义对齐**：子代理 `AgentRunSpec.finalize_on_max_iterations` 硬编码
  `False`（对齐 nanobot，无对应 config 字段）。
- **E4 — 收尾文案对齐**：子代理 `AgentRunSpec.max_iterations_message` 硬编码为 nanobot 同款
  `"Task completed but no final response was generated."`（无对应 config 字段）。

## 4. 范围与约束

- 不引入新的 config 字段；G3/G4 按 nanobot 硬编码（经用户确认「对齐 nanobot 硬编码」）。
- 不改动 `main.py` 接线：配置已在 `SubagentManager.__init__` 从原始 `config` 提取
  （同 step116 `_extract_disabled_skills` 的 duck-typed `getattr` 链）。
- 不回归既有子代理测试：构造 `ContextGovernanceConfig` 时需保留 runner 默认
  `context_window_tokens=200_000` 预算，否则会触发全量工具结果摘要破坏既有行为。
