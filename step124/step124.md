# step124 配套文档：spawn temperature 覆写（G7）

## 1. 问题背景

step120–123 已对齐子代理运行配置传播（G1–G4）、announce 模板化/origin_message_id（G6/G8）、
runtime 逐父同步（G5）、ToolContext 沙箱 + 多相位状态（G9/G10）。对照 nanobot `SubagentManager`
路线图 §6 仅剩一项差距：

- **G7（spawn temperature 覆写）**：nanobot 的 `spawn(task, temperature=None, ...)` 在
  `temperature is not None` 时执行 `runtime = runtime.with_generation_overrides(temperature=temperature)`，
  使子代理以覆写后的生成温度运行。learn_nano 的 `SubagentManager.spawn` 与 `SpawnTool` 均不支持
  `temperature` 覆写，子代理只能沿用父 runtime 默认温度，无法按子任务调节探索/确定性强度。

## 2. 本 step 解决的问题与原因

**解决**：允许在 spawn 子代理时覆写其运行 temperature，对齐 nanobot G7。父代理可针对子任务
（如「严谨检索」降温度、「发散创意」升温度）微调子代理生成随机性。

**为什么这样做**：生成温度直接影响子代理输出质量与一致性；nanobot 已将此作为 spawn 的一等参数，
是子代理可编排性的组成部分。实现上完全复用 step122（G5）既有的「runtime → temperature 衍生」
通道，改动面极小。

## 3. 原理思路与具体实现

### 3.1 调研关键事实

- `LLMRuntime`（step124/llm.py:27）是 `frozen` dataclass，**无** `with_generation_overrides`；
  `temperature`/`max_tokens` 为读取 `generation` 的 property。
- step122（G5）已让 `_run_subagent` 从 `origin["runtime"]` 衍生 `temperature` 注入 `AgentRunSpec`：
  `temperature = getattr(runtime, "temperature", 0.7)`。因此只要把「覆写后的 runtime」放回
  `origin["runtime"]`，覆写即自动生效，**无需改动 `_run_subagent`**。

### 3.2 实现（3 文件）

- **F1 — `llm.py`**：`LLMRuntime` 新增 `with_generation_overrides(temperature=None, max_tokens=None)`
  方法，返回 generations 参数被覆写后的**新** `LLMRuntime`（provider/model/context_window_tokens
  沿用，原对象不变）。
- **F2 — `subagent.py`**：`spawn` 增加 `temperature: float | None = None` 形参；非空时对
  `origin["runtime"]`（为 None 时以 `self._provider` 合成最小 `LLMRuntime` 兜底）执行
  `with_generation_overrides(temperature=temperature)` 并写回 `origin["runtime"]`。
- **F3 — `tools/spawn.py`**：`tool_parameters_schema` 增加 `temperature=NumberSchema(0.7,
  minimum=0.0, maximum=2.0)`；`execute` 接纳并透传给 `manager.spawn`。

### 3.3 不改之处

- `runner.py` 与 `SubagentManager._run_subagent` 主逻辑不动；覆写经 G5 既有通道生效。
- `temperature=None` 时行为与 step123 完全一致。

## 4. 目标与实现

- **目标**：对齐 nanobot G7，spawn 支持 temperature 覆写。
- **实现**：LLMRuntime 覆写方法 + spawn 形参 + SpawnTool 参数暴露；无回归（全量失败数与
  step123 基线持平 25）。

## 5. 核心函数/类功能说明

- `LLMRuntime.with_generation_overrides`（llm.py）：不可变覆写生成参数，返回新实例。
- `SubagentManager.spawn` 的 `temperature` 形参（subagent.py）：在合并 origin 后覆写 runtime。
- `SpawnTool.execute` 的 `temperature` 参数（tools/spawn.py）：向 LLM 暴露并透传覆写。

## 6. 暴露的问题 / 刻意遗留

- 仅覆写 `temperature`（G7 范围）；`with_generation_overrides` 顺带支持 `max_tokens` 以对齐
  nanobot 方法签名，但 step124 不通过 spawn 暴露 max_tokens 覆写（留待将来按需扩展）。

## 7. 下一 step 待解决

- **step「通道清洗」**（G6 通道部分，独立 step）：清洗 announce 正文（保留 LLM 全文注入），
  此前 step121 已推迟。
- 可选：spawn 暴露 `max_tokens` 覆写；主循环 `ToolContext` 注入 `workspace_sandbox`（G9 parity）。
- 至此路线图 §6（step120–124）规划项**全部完成**，子代理子系统已高度对齐 nanobot。
