# step123 需求定义：子代理 ToolContext 沙箱 + 多相位状态（G9 + G10）

## 1. 问题背景

step122 已完成 runtime（模型/生成参数）逐父同步（G5）。对照 nanobot `SubagentManager`
仍有两个「运行期上下文」差距（路线图 §6）：

- **G9（ToolContext workspace_sandbox）**：nanobot 在子代理 `_build_tools` 构造
  `ToolContext` 时注入 `workspace_sandbox=workspace_sandbox_status(...)`（subagent.py:211），
  使子代理工具可感知宿主的 workspace 限制级别（off/system/application）。learn_nano 的
  `ToolContext`（context.py:87）**无此字段**，且子代理 `_build_tools` 未注入，子代理工具
  拿不到沙箱状态。
- **G10（多相位 status.phase）**：nanobot 用 `checkpoint_callback` 把 runner 每次迭代的
  `phase` 同步到 `SubagentStatus.phase`，使父代理可观测子代理处于
  `initializing / awaiting_tools / tools_completed / final_response` 等中间相位。
  learn_nano 的 `SubagentStatus` 仅 `after_iteration` 钩子更新 `iteration`，`phase` 仍只在
  结束时被置为 `done`/`error`，运行期对父代理不透明。

经调研确认：learn_nano 的 `AgentRunSpec` 已支持 `checkpoint_callback`（runner.py:117），
且 runner 在迭代中**已发出** `awaiting_tools` / `tools_completed` / `final_response` /
`after max_iterations` 等相位 checkpoint（runner.py:1176/1229/1335/1365）。因此 G10 仅需
在 `_run_subagent` 接好 `checkpoint_callback` 闭包即可，无需改动 runner。

经用户确认，step123 的 G9 **仅子代理**（不改动主循环 `loop.py:1251` 的 `ToolContext`），
保持最小增量。

## 2. 目标

对齐 nanobot 子代理运行期上下文：
- G9：子代理工具装配上下文 `ToolContext` 携带 `workspace_sandbox` 状态。
- G10：子代理 `SubagentStatus.phase` 随 runner 迭代实时反映多相位，可被父代理观测。

## 3. 需求定义（最小增量）

- **F1 — ToolContext 加字段**：`ToolContext` 新增 `workspace_sandbox: Any | None = None`
  （默认 `None`，向后兼容既有构造，主循环不改）。
- **F2 — 子代理注入 sandbox**：`SubagentManager._build_tools` 构造 `ToolContext` 时注入
  `workspace_sandbox=workspace_sandbox_status(restrict_to_workspace=self._restrict_to_workspace,
  workspace=root)`，其中 `root` 与既有 `workspace` 字段同源。
- **F3 — checkpoint 驱动相位**：`SubagentManager._run_subagent` 定义
  `_on_checkpoint(payload)` 闭包，将 `payload["phase"]`（及 `iteration`）同步到
  `status.phase`（及 `status.iteration`），并作为 `checkpoint_callback` 传入 `AgentRunSpec`。

## 4. 范围与约束

- 不改动 `runner.py`（已支持 `checkpoint_callback` 与多相位发出）。
- 不改动 `loop.py` 主循环的 `ToolContext`（G9 仅子代理；主循环 parity 留作后续 step）。
- `workspace_sandbox_status` 调用沿用既有 `restrict_to_workspace` 与 `workspace`，不引入
  新的限制语义；其三级状态（off/system/application）由该函数内部决定。
- `iteration` 已由 `_SubagentHook.after_iteration` 更新；F3 再次同步为幂等冗余、对齐 nanobot。
