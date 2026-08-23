# step120 架构设计：子代理运行配置传播

## 1. 总体设计

在现有 `SubagentManager`（step119 已具备 scope 隔离、RequestContext/workspace_scope 绑定、
`llm_timeout_s` 同步）之上，新增**两处**最小改动：

1. **配置提取（构造期）**：`__init__` 从原始 `config` 经 duck-typed `getattr` 链提取
   `agents.defaults.max_tool_result_chars` 与 `agents.defaults.fail_on_tool_error`，
   存入 `self._max_tool_result_chars` / `self._fail_on_tool_error`（缺省回退 16_000 / True）。
2. **运行期注入**：`_run_subagent` 构造 `AgentRunSpec` 时，新增 4 个字段，把上述运行限制与
   nanobot 一致项一并写入。

## 2. 关键原理

### 2.1 为何走 `governance_config` 而非直传 `max_tool_result_chars`

`AgentRunSpec` 上**没有** `max_tool_result_chars` 直传字段；工具结果截断发生在
`ContextGovernor.normalize_tool_result(config, ...)` 中读取 `config.max_tool_result_chars`。
因此必须构造 `ContextGovernanceConfig` 并赋给 `AgentRunSpec.governance_config`。

### 2.2 `ContextGovernanceConfig` 构造约束（踩坑点）

`ContextGovernanceConfig` 是普通类（非 dataclass），**首参 `tools` 为必填位置参数**，且
`context_window_tokens` 默认 `None`。而 `AgentRunner._resolve_gov_config` 在
`spec.governance_config is None` 时会构造默认实例：

```python
ContextGovernanceConfig(
    tools=spec.tools,
    context_window_tokens=spec.context_window_tokens or 200_000,
    max_tokens=spec.max_tokens,
)
```

若我们传入的 `governance_config` 未带 `context_window_tokens`，runner 会**直接采用该实例**
（`spec.governance_config is not None` 分支），导致 `context_window_tokens=None`。此时
`input_budget()` 返回 0，`apply_tool_result_budget` 会把**所有** inflight 工具结果摘要压缩，
破坏既有子代理行为（如递归 spawn 测试中 "spawn not found" 结果被清空）。

**对策**：构造 `ContextGovernanceConfig` 时显式传入 `tools=tools`、
`context_window_tokens=200_000`、`max_tokens=4096`，精确复刻 runner 默认，仅覆盖
`max_tool_result_chars`。

### 2.3 工具集构建复用

原本 `AgentRunSpec(tools=self._build_tools())` 每次新建工具注册表。本次改为先
`tools = self._build_tools()` 构建一次，同一实例复用于 `AgentRunSpec.tools` 与
`ContextGovernanceConfig.tools`（compact/预算逻辑需要 `tools` 定义），避免重复构建，行为等价。

### 2.4 G3/G4 硬编码决策

`finalize_on_max_iterations` 与 `max_iterations_message` 在 learn_nano 无对应 config 字段。
经用户确认采用「对齐 nanobot 硬编码」：

- `finalize_on_max_iterations=False`：子代理 max-iterations 边界由隐形续跑接管，不生成用户可见
  fallback；runner 不再产出收尾响应（nanobot 同款）。
- `max_iterations_message="Task completed but no final response was generated."`：nanobot 子代理
  同款文案（learn_nano 自有 `_MAX_ITERATIONS_FALLBACK` 为
  `"Reached max iterations without a final response."`，此处对齐 nanobot）。

## 3. 接口契约（详见 api-spec.md）

- `SubagentManager.__init__(config=...)` 新增内部属性 `_max_tool_result_chars: int`、
  `_fail_on_tool_error: bool`（从 `config` 提取，缺省 16_000 / True）。
- `_run_subagent` 产出的 `AgentRunSpec` 满足：
  - `governance_config.max_tool_result_chars == self._max_tool_result_chars`
  - `fail_on_tool_error == self._fail_on_tool_error`
  - `finalize_on_max_iterations is False`
  - `max_iterations_message == "Task completed but no final response was generated."`

## 4. 测试策略

复用 step117 的假 runner harness（`mgr.runner.run = fake_run`，捕获 spec），断言 spec 字段；
另加 `_extract_*` 提取函数的单测。既有 `test_spawned_subagent_cannot_recursive_spawn` 因
`fail_on_tool_error` 现随配置生效（默认 True，子代理遇工具错误提前终止）而由「2 次 LLM 调用 +
not found 内容」改为断言**结构性安全不变量**（spawn 不在子代理工具集 + 无嵌套任务），不受调用次数
实现细节影响。
