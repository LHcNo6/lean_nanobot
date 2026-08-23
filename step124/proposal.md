# step124 需求定义：spawn temperature 覆写（G7）

## 1. 问题背景

step123 已完成子代理 ToolContext 沙箱（G9）+ 多相位状态（G10）。对照 nanobot `SubagentManager`
仍有最后一项差距（路线图 §6）：

- **G7（spawn temperature 覆写）**：nanobot 的 `spawn(task, temperature=None, ...)` 在
  `temperature is not None` 时执行 `runtime = runtime.with_generation_overrides(temperature=temperature)`，
  使子代理以父会话 runtime 的 generation 参数（但被覆写的 temperature）运行。learn_nano 的
  `SubagentManager.spawn` 与 `SpawnTool` 均不支持 `temperature` 覆写，子代理只能沿用父 runtime 的
  默认 temperature（或 G5 继承值），无法按任务微调探索/确定性强度。

经调研确认（step124 调研）：
- nanobot 依赖 `LLMRuntime.with_generation_overrides`，而 learn_nano 的 `LLMRuntime`
  （step124/llm.py:27）**没有**该方法（仅有 `capture` 与 `temperature`/`max_tokens` property）。
- step122（G5）已让 `_run_subagent` 从 `origin["runtime"]` 衍生 `temperature`，故只要把覆写后的
  runtime 放回 `origin["runtime"]`，覆写即可自动生效，**无需改动 `_run_subagent`**。

## 2. 目标

对齐 nanobot G7：允许在 spawn 子代理时覆写其运行 temperature，使父代理可针对子任务调节生成随机性。

## 3. 需求定义（最小增量）

- **F1 — LLMRuntime 支持覆写**：`LLMRuntime` 新增 `with_generation_overrides(temperature=None,
  max_tokens=None)` 方法，返回 generations 参数被覆写后的**新** `LLMRuntime`（provider/model/
  context_window_tokens 等沿用，原对象不变）。
- **F2 — spawn 接纳 temperature**：`SubagentManager.spawn` 增加 `temperature: float | None = None`
  形参；非空时以 `origin["runtime"].with_generation_overrides(temperature=temperature)` 覆写并写回
  `origin["runtime"]`（runtime 为 None 时以 `self._provider` 合成最小 `LLMRuntime` 兜底）。
- **F3 — SpawnTool 暴露参数**：`SpawnTool` 的 `tool_parameters_schema` 增加 `temperature`
  （`NumberSchema`，约束 0.0–2.0），`execute` 接纳并透传给 `manager.spawn`。

## 4. 范围与约束

- 不改 `runner.py` 与 `SubagentManager._run_subagent` 主逻辑（G5 已让 temperature 从 runtime 衍生）。
- 仅覆写 `temperature`（G7 范围）；`with_generation_overrides` 顺带支持 `max_tokens` 以对齐
  nanobot 方法签名，但 step124 仅通过 `spawn` 暴露 temperature。
- `temperature=None` 时行为与 step123 完全一致（无回归）。
