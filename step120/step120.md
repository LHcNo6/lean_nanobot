# step120：子代理运行配置传播（对齐 nanobot）

## 1. 问题背景

step110–119 已对齐子代理核心九维度（并发/隔离/取消/受限运行/上下文绑定/压缩/可观测）。
step119 末的差距分析（路线图 §6）指出：子代理 `AgentRunSpec` **运行期行为配置**仍落后于
nanobot，对应 G1–G4：

- 父配置 `agents.defaults.max_tool_result_chars` 未生效（子代理沿用 runner 默认）；
- 父配置 `agents.defaults.fail_on_tool_error` 未生效（子代理沿用 `AgentRunSpec` 默认 `False`）；
- `finalize_on_max_iterations` 语义与 nanobot 子代理相反（`True` vs `False`）；
- `max_iterations_message` 文案与 nanobot 子代理不一致。

其中 G1/G2 有 config 字段可提取，G3/G4 在 learn_nano 无对应字段（nanobot 对子代理硬编码）。

## 2. 这一 step 解决了什么 / 为什么

把父配置的运行限制传播到子代理 `AgentRunSpec`，使子代理运行行为真正受父配置与 nanobot 约束。
这是「增强/打磨」层里**唯一影响子代理实际运行行为正确性**的缺口，优先级最高。

方案取舍（经用户确认「对齐 nanobot 硬编码」）：
- G1/G2 从 `config.agents.defaults` 提取并注入；
- G3/G4 无 config 字段，直接硬编码为 nanobot 同款值（`False` + 固定文案）。

## 3. 原理思路与具体实现

- **提取（构造期）**：新增 `_extract_max_tool_result_chars` / `_extract_fail_on_tool_error`，
  复用 step116 `_extract_disabled_skills` 的 duck-typed `getattr` 链（支持完整 Config / 扁平
  duck-view / `None`），缺省回退 16_000 / True；结果存入 `self._max_tool_result_chars` /
  `self._fail_on_tool_error`。
- **注入（运行期）**：`_run_subagent` 中先 `tools = self._build_tools()`，再构造
  `AgentRunSpec`，新增 4 字段：
  - `governance_config=ContextGovernanceConfig(tools=tools, max_tool_result_chars=..., context_window_tokens=200_000, max_tokens=4096)`
  - `fail_on_tool_error=self._fail_on_tool_error`
  - `finalize_on_max_iterations=False`
  - `max_iterations_message="Task completed but no final response was generated."`

**关键坑（已在 design.md 详述）**：`ContextGovernanceConfig` 首参 `tools` 必填且
`context_window_tokens` 默认 `None`。若仅传 `max_tool_result_chars` 而令
`context_window_tokens=None`，runner 会直接采用该实例，导致 `input_budget()==0` 把全部 inflight
工具结果摘要压缩，破坏既有行为。因此显式传入 `context_window_tokens=200_000` / `max_tokens=4096`
**复刻 runner 默认**，仅覆盖截断阈值。

## 4. 目标与实现结果

- 子代理工具结果按 `config.agents.defaults.max_tool_result_chars` 截断（E1）；
- 子代理 `fail_on_tool_error` 随 `config.agents.defaults.fail_on_tool_error` 生效（E2）；
- 子代理 `finalize_on_max_iterations=False`（E3，对齐 nanobot）；
- 子代理 `max_iterations_message` 对齐 nanobot 文案（E4）。

`governance.py` / `runner.py` / `main.py` 均无改动；仅 `subagent.py` 新增提取与注入逻辑。

## 5. 核心函数 / 类功能说明

- `_extract_max_tool_result_chars(config) -> int`：安全提取截断阈值（缺省 16_000）。
- `_extract_fail_on_tool_error(config) -> bool`：安全提取工具错误升级开关（缺省 True）。
- `SubagentManager.__init__`：新增两内部属性保存上述值。
- `SubagentManager._run_subagent`：`AgentRunSpec` 新增 `governance_config` / `fail_on_tool_error`
  / `finalize_on_max_iterations` / `max_iterations_message` 四项。

## 6. 暴露了什么问题

- learn_nano 的 `ContextGovernanceConfig` 构造与 runner 默认存在「隐式契约」：
  `governance_config=None` 时由 runner 补全 `context_window_tokens=200_000`；一旦显式传入则
  必须手动补齐预算字段，否则行为剧变。后续 step（如 step122 runtime 同步）若再触碰
  `governance_config`，需牢记此约束。
- `finalize_on_max_iterations` 与 `max_iterations_message` 在 learn_nano 无配置入口，
  语义依赖硬编码；若函数级可配置需求出现，应考虑在 `AgentDefaults` 增加字段（见下一步）。

## 7. 下一 step 要解决什么

- **step121（announce 模板化 + origin_message_id + 通道清洗，G6+G8）**：`_announce` 改用
  `subagent_announce.md` 模板渲染，透传 `origin_message_id`，引入 `subagent_channel_display`
  清洗 body，提升父代理可读性。
- 后续 **step122（runtime/model 同步 G5）**、**step123（workspace_sandbox + 相位粒度 G9+G10）**、
  **step124（spawn temperature 覆写 G7）** 依次推进；属打磨项，按需。
