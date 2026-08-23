# step122 需求定义：子代理 runtime（模型/生成参数）逐父同步

## 1. 问题背景

step121 已完成子代理 announce 模板化与 origin_message_id 透传（G6 / G8）。
路线图 §6 差距分析指出，子代理运行期配置与 nanobot 仍有以下差距：

- **G5（runtime 逐父同步）**：nanobot 的子代理通过 `spawn(runtime=...)` 把父会话的
  `LLMRuntime` 逐层透传，`_run_subagent` 将整份 runtime 交给 `AgentRunSpec`，使子代理
  继承父会话的 **provider / model / 生成参数（temperature、max_tokens）**。
  learn_nano 的子代理使用共享的 `self._provider`，且 `AgentRunSpec` 仅用标量
  `model=None / temperature=0.7 / max_tokens=4096`，**未从父会话 runtime 继承 model 与
  生成参数**，导致父子模型/温度/截断长度不一致。

经调研确认（step122 调研）：
- `LLMRuntime` 暴露 `provider` / `model` / `temperature`（property → `generation.temperature`）
  / `max_tokens`（property → `generation.max_tokens`）。
- `runner.py:660-663` 会把 `spec.model` / `spec.temperature` / `spec.max_tokens` 直接转发给
  `provider.chat_with_retry(...)`，因此改写这些标量即对 LLM 调用生效。
- 生产接线（`main.py`）：`SubagentManager(provider=snapshot.provider)` 与
  `AgentLoop(runtime=LLMRuntime.capture(provider=snapshot.provider, ...))` 中的
  `self._provider` 与 `runtime.provider` 是**同一对象**，故子代理沿用 `self._provider`
  在终态行为上与「继承 runtime.provider」等价。

经用户确认，step122 采用 **最小增量方案（衍生标量）**：在 `_run_subagent` 内从
`origin["runtime"]` 衍生 `model` / `temperature` / `max_tokens` 注入 `AgentRunSpec`，
`provider` 沿用 `self._provider`。**不新增 `AgentRunSpec.runtime` 字段、不改 runner**。

## 2. 目标

对齐 nanobot G5「runtime 逐父同步」：子代理继承父会话的 **model 与生成参数**
（temperature / max_tokens），使父子会话在模型与生成控制上保持一致。

## 3. 需求定义（最小增量）

- **F1 — model 逐父同步**：当 `origin["runtime"]` 存在且 `runtime.model` 非空时，
  `AgentRunSpec.model = runtime.model`；否则保持缺省 `None`。
- **F2 — temperature 逐父同步**：当 `origin["runtime"]` 存在时，
  `AgentRunSpec.temperature = runtime.temperature`；否则缺省 `0.7`。
- **F3 — max_tokens 逐父同步**：当 `origin["runtime"]` 存在时，
  `AgentRunSpec.max_tokens = runtime.max_tokens`；否则缺省 `4096`。
- **F4 — provider 沿用 manager 自身**：`AgentRunSpec.provider = self._provider`
  （生产环境与 `runtime.provider` 同对象；仅当 `self._provider` 为 `None` 时回退
  `runtime.provider`，避免无 provider 直接返回）。

## 4. 范围与约束

- 不改动 `runner.py` 与 `AgentRunSpec` 字段定义；不引入 `runtime` 字段。
- 不改 `main.py` 接线（生产 `self._provider == runtime.provider` 已同对象）。
- **刻意遗留**（记录于 step122.md）：
  - `context_window_tokens` 仍用 step120 的 `200_000`（避免回归，不在本 step 同步）。
  - `provider` 取 `self._provider` 而非 `runtime.provider`（生产同对象；保留 manager
    显式注入 provider 的能力以兼容既有测试）。
- 不实现 spawn 级 temperature 覆写（G7，推迟至独立 step124）。
