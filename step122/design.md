# step122 架构设计：子代理 runtime（模型/生成参数）逐父同步

## 1. 总体设计

在 `SubagentManager._run_subagent` 的入口处，从 `origin["runtime"]`（已由 spawn 工具在
step121 透传）衍生子代理运行规格的 `model` / `temperature` / `max_tokens`，并将它们与
`self._provider` 一并注入 `AgentRunSpec`。

核心原则：**最小增量 + 零回归**。复用既有 `origin["runtime"]` 透传通道与既有
`AgentRunSpec` 标量字段，不新增 `runtime` 字段、不改 `runner.py`。

## 2. 关键原理

### 2.1 `LLMRuntime` 结构（step122/llm.py:27）

```python
class LLMRuntime:
    provider: Any                      # 底层 provider 实例
    model: str                         # 模型名（无则 ""）
    generation: GenerationSettings     # 含 temperature / max_tokens
    context_window_tokens: int | None
    @property
    def temperature(self) -> float: ...   # 读 generation.temperature
    @property
    def max_tokens(self) -> int: ...      # 读 generation.max_tokens
```

因此子代理要继承父会话生成参数，只需读取 `runtime.model` / `runtime.temperature` /
`runtime.max_tokens`。

### 2.2 runner 转发（step122/runner.py:660-663）

```python
response = await spec.provider.chat_with_retry(
    model=spec.model, temperature=spec.temperature, max_tokens=spec.max_tokens, ...
)
```

`AgentRunSpec` 已有 `model`（缺省 `None`）、`temperature`（缺省 `0.7`）、
`max_tokens`（缺省 `4096`）三个标量字段，改写它们即对 LLM 调用生效。

### 2.3 生产接线等价性（step122/main.py:104-112, 120-124）

```python
subagent_manager = SubagentManager(provider=snapshot.provider, ...)
runtime = LLMRuntime.capture(provider=snapshot.provider, ...)
agent_loop = AgentLoop(runtime=runtime, ...)
```

`self._provider`（subagent）与 `runtime.provider`（loop）均为 `snapshot.provider` ——
**同一对象**。故子代理沿用 `self._provider` 在终态上与「继承 runtime.provider」行为一致，
这也是本 step 不必改动 provider 字段的原因。

### 2.4 为什么不改 runner / 不新增 runtime 字段

- 改 runner 需重构 `AgentRunSpec` → 在 `run()` 内用 runtime 派生 provider/model，
  改动面更大、回归风险更高。
- 衍生标量方案复用既有字段与转发链路，实现聚焦在 `_run_subagent` 一处，符合「每步只增加
  一个最简功能」。

## 3. 接口契约（摘要，详见 api-spec.md）

- 输入：`origin["runtime"]`（`LLMRuntime | None`）。
- 输出：进入 `AgentRunSpec` 的 `provider / model / temperature / max_tokens` 四字段取值规则。
- 不变量：`provider` 缺省回退、`None` 时早退；无 runtime 时退化为既有标量缺省。

## 4. 改动文件清单

- `step122/subagent.py`：`_run_subagent` 入口衍生四字段并注入 `AgentRunSpec`。
- `step122/main.py`：零改动。
- `step122/runner.py`、`AgentRunSpec`：零改动。
- 新增 `step122/tests/test_subagent_runtime_sync.py`：假 runner 捕获 spec 断言。

## 5. 测试策略

- **`test_run_spec_inherits_runtime_gen_settings`**：构造含
  `model="m1" / temperature=0.3 / max_tokens=2048` 的 runtime-like 对象放入 `origin`，
  断言 `spec.model == "m1"`、`spec.temperature == 0.3`、`spec.max_tokens == 2048`，
  且 `spec.provider is self._provider`。
- **`test_run_spec_defaults_when_no_runtime`**：`origin` 无 runtime，断言
  `spec.model is None`、`spec.temperature == 0.7`、`spec.max_tokens == 4096`、
  `spec.provider is self._provider`。
- 既有测试（含 `test_subagent_announce_injected_mid_turn`）不得回归：
  provider 仍取 `self._provider`，仅 model/生成参数随父 runtime 变化。

## 6. 验收标准

- 全量 `step122/tests` 失败数与 step121 基线（25）持平。
- 新增用例全绿。
- 配套 `step122.md` + 三份规范齐备，提交并推送。
