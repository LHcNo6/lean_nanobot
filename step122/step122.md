# step122 配套文档：子代理 runtime（模型/生成参数）逐父同步

## 1. 问题背景

step110–121 已把子代理系统在工具隔离、上下文绑定、运行配置传播、announce 模板化、
origin_message_id 透传等方面对齐到 nanobot。但对照 nanobot `SubagentManager` 仍有一处
「运行期配置」差距（路线图 §6 G5）：

- **nanobot**：子代理通过 `spawn(runtime=...)` 把父会话的 `LLMRuntime` 逐层透传到
  `_run_subagent`，并以整份 `runtime` 构造 `AgentRunSpec`，使子代理**继承父会话的
  provider / model / 生成参数（temperature、max_tokens）**。
- **learn_nano（step121 及之前）**：子代理使用共享的 `self._provider`，且
  `AgentRunSpec` 仅用标量 `model=None / temperature=0.7 / max_tokens=4096`，
  **未从父会话 runtime 继承 model 与生成参数**，导致父子模型/温度/截断长度可能不一致。

## 2. 本 step 解决的问题与原因

**解决**：让子代理继承父会话的 **model 与生成参数**（temperature / max_tokens），与
nanobot 的「runtime 逐父同步」语义对齐，使父子会话在模型与生成控制上保持一致。

**为什么这样做**：
- 多模型/多温度编排场景（如父代理用强模型、子代理用轻模型，或父代理调高 temperature
  探索、子代理降温度稳定）下，子代理若不继承父设置，会出现「配置漂移」。
- 这是 nanobot 子代理行为正确性的一个维度，属于「增强/打磨」层，但影响运行期语义，
  优先级高于纯展示类对齐项。

## 3. 原理思路与具体实现

### 3.1 调研结论（关键事实）

- `LLMRuntime`（step122/llm.py:27）暴露 `provider` / `model` / `temperature`（property →
  `generation.temperature`）/ `max_tokens`（property → `generation.max_tokens`）。
- `runner.py:660-663` 会把 `spec.model` / `spec.temperature` / `spec.max_tokens` 转发给
  `provider.chat_with_retry(...)`，因此改写这些标量即对 LLM 调用生效。
- 生产接线（step122/main.py）：`SubagentManager(provider=snapshot.provider)` 与
  `AgentLoop(runtime=LLMRuntime.capture(provider=snapshot.provider, ...))` 中，
  `self._provider`（subagent）与 `runtime.provider`（loop）**是同一对象**。

### 3.2 方案选择（最小增量，衍生标量）

经与用户确认，采用**衍生标量**方案而非「给 `AgentRunSpec` 加 `runtime` 字段 + 改 runner」：

- 在 `SubagentManager._run_subagent` 入口从 `origin["runtime"]`（step121 已透传）衍生
  `model` / `temperature` / `max_tokens`，连同 `provider=self._provider` 注入 `AgentRunSpec`。
- **不改 `runner.py`、不新增 `AgentRunSpec.runtime` 字段**。
- 终态行为等价：生产环境 `self._provider == runtime.provider`，故 provider 沿用
  `self._provider` 与「继承 runtime.provider」完全一致；model/生成参数则真正取自父 runtime。

### 3.3 具体改动（step122/subagent.py）

```python
# _run_subagent 入口
runtime = origin.get("runtime") if origin else None
provider = self._provider or (getattr(runtime, "provider", None) if runtime else None)
if provider is None:
    return
...
result = await self.runner.run(AgentRunSpec(
    ...
    # step122（G5）：provider 沿用 manager 自身（生产 == runtime.provider）；
    # model / temperature / max_tokens 继承父会话 runtime，缺省退化为标量缺省。
    # 用 getattr 防御 runtime 未实现这些属性的情况（如测试传入的占位对象）。
    provider=provider,
    model=getattr(runtime, "model", None) or None,
    temperature=getattr(runtime, "temperature", 0.7),
    max_tokens=getattr(runtime, "max_tokens", 4096),
    ...
))
```

- 防御式 `getattr(..., default)`：当 `origin["runtime"]` 为裸占位对象（如既有测试传入的
  `object()`）时不会抛 `AttributeError`，退化为标量缺省。
- `provider` 早退守卫扩展为「`self._provider` 或回退 `runtime.provider` 任一非 None」。

## 4. 目标与实现

- **目标**：对齐 nanobot G5，子代理继承父会话的 model + 生成参数。
- **实现**：`_run_subagent` 衍生四字段注入 `AgentRunSpec`；无回归（全量失败数与 step121
  基线持平 25）。

## 5. 核心函数/类功能说明

- `SubagentManager._run_subagent`（step122/subagent.py）：后台协程。新增 G5 段——
  从 `origin["runtime"]` 解析 `provider/model/temperature/max_tokens` 并传入
  `AgentRunSpec`；其余职责（工具集裁剪、RequestContext 绑定、workspace_scope、llm_timeout
  同步、治理配置传播）不变。
- `LLMRuntime`（参考，未改动）：提供父会话 provider/model/generation 信息，是 G5 数据来源。

## 6. 暴露的问题 / 刻意遗留

- **`context_window_tokens` 未同步**：治理配置仍用 step120 的 `200_000`（避免回归），
  未随父 runtime 的 `context_window_tokens` 变化。可作为后续 refinement。
- **`provider` 取 `self._provider` 而非 `runtime.provider`**：生产同对象，行为等价；
  仅当 `self._provider` 为 None 时回退 `runtime.provider`。若未来生产环境为子代理配置
  了与父 runtime 不同的 provider，则该差异需另行处理（届时再考虑 `runtime` 字段方案）。
- **不在本 step 做 spawn 级 temperature 覆写**（G7）：属 step124 范畴。

## 7. 下一 step 待解决

- **step「通道清洗」（G6 通道部分，独立 step）**：实现 `utils/subagent_channel_display.py`
  的 `scrub_subagent_announce_body`，在展示边界清洗 announce 正文（保留 LLM 全文注入），
  step121 已推迟此部分。
- **step123（G9+G10）**：子代理 `ToolContext` 注入 `workspace_sandbox`、相位粒度
  `status.phase` 多相位更新。
- **step124（G7）**：`spawn` 支持 `temperature` 覆写（`runtime.with_generation_overrides`）。
