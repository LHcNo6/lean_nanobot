# step123 配套文档：子代理 ToolContext 沙箱 + 多相位状态（G9 + G10）

## 1. 问题背景

step120–122 已对齐子代理运行配置传播、announce 模板化/origin_message_id、runtime 逐父同步。
对照 nanobot `SubagentManager` 仍有两处「运行期上下文」差距（路线图 §6）：

- **G9**：nanobot 子代理 `_build_tools` 构造 `ToolContext` 时注入
  `workspace_sandbox=workspace_sandbox_status(...)`（subagent.py:211），使子代理工具可感知
  宿主 workspace 限制级别（off / system / application）。learn_nano 的 `ToolContext`
  无此字段，子代理工具拿不到沙箱状态。
- **G10**：nanobot 用 `checkpoint_callback` 把 runner 每轮迭代的 `phase` 同步到
  `SubagentStatus.phase`，父代理可观测子代理处于 `initializing / awaiting_tools /
  tools_completed / final_response` 等中间相位；learn_nano 子代理 `phase` 仅在结束置
  `done`/`error`，运行期对父代理不透明。

## 2. 本 step 解决的问题与原因

**解决**：补齐子代理运行期上下文的两个维度——工具可见的 workspace 沙箱状态、对父代理可观测的
多相位运行状态。两者均为 nanobot 子代理可观测性/正确性的组成部分，属「增强/打磨」层。

**为什么这样做**：
- G9：工具（尤其是文件/exec 类）需感知宿主限制级别，才能给出与父代理一致的权限提示与
  安全语义；缺失会导致子代理与父代理在沙箱认知上不一致。
- G10：父代理（及 `self`/`my` 工具）查询子代理状态时，仅 `done`/`error` 两态不足以反映
  「正在等工具 / 工具执行中」等中间状态，多相位提升可观测性与调试体验。

## 3. 原理思路与具体实现

### 3.1 调研关键事实

- `AgentRunSpec.checkpoint_callback`（runner.py:117）**已支持**；runner 在迭代循环里已通过
  `_emit_checkpoint` 发出含 `phase`/`iteration` 的 payload：`awaiting_tools`（:1176）、
  `tools_completed`（:1229）、`final_response`（:1335）、`after max_iterations`（:1365）。
- `SubagentStatus`（subagent.py:164）已有 `phase`（初值 `initializing`）与 `iteration` 字段；
  `_SubagentHook.after_iteration`（:183）已更新 `iteration`/`tool_events`/`usage`。
- `workspace_sandbox_status(restrict_to_workspace, workspace, environ=None)`
  （security/workspace_access.py:212）已存在，返回 `WorkspaceSandboxStatus`。

### 3.2 实现

- **G9（F1+F2）**：`context.py` 的 `ToolContext` 新增 `workspace_sandbox: Any | None = None`
  （默认 `None`，向后兼容）；`subagent.py` 的 `_build_tools` 构造 `ToolContext` 时额外传入
  `workspace_sandbox=workspace_sandbox_status(restrict_to_workspace=self._restrict_to_workspace,
  workspace=root)`。
- **G10（F3）**：`_run_subagent` 内定义 `_on_checkpoint(payload)` 闭包，将
  `payload["phase"]`（及 `iteration`）同步到 `status.phase`（及 `status.iteration`），
  并以 `checkpoint_callback=_on_checkpoint` 传入 `AgentRunSpec`。

### 3.3 刻意遗留 / 范围

- **G9 仅子代理**：主循环 `loop.py:1251` 的 `ToolContext` 构造未改（保持 `None`），
  主循环 parity 留作后续 step。经用户确认，step123 仅对齐子代理。
- **不改 runner.py / loop.py**：G10 完全复用 runner 既有 checkpoint 机制。

## 4. 目标与实现

- **目标**：对齐 nanobot G9（子代理 ToolContext 沙箱）+ G10（多相位状态）。
- **实现**：`context.py` 加字段 + `subagent.py` 注入与 checkpoint 接线；无回归（全量失败数与
  step122 基线持平 25）。

## 5. 核心函数/类功能说明

- `ToolContext.workspace_sandbox`（context.py）：新增可选字段，承载宿主沙箱状态。
- `SubagentManager._build_tools`（subagent.py）：构造 `ToolContext` 时填充 `workspace_sandbox`。
- `SubagentManager._run_subagent._on_checkpoint`（subagent.py）：checkpoint 闭包，驱动
  `status.phase` 多相位流转；终态仍由成功/异常分支置 `done`/`error`。

## 6. 暴露的问题 / 刻意遗留

- 主循环 `ToolContext` 未注入 `workspace_sandbox`（与子代理不一致）——后续 step 可补齐 parity。
- `iteration` 由 `_SubagentHook.after_iteration` 与 `_on_checkpoint` 双重同步（幂等冗余，
  对齐 nanobot 的 `_on_checkpoint` 行为）。

## 7. 下一 step 待解决

- **step「通道清洗」**（G6 通道部分）：独立 step，清洗 announce 正文（保留 LLM 全文注入）。
- **step124（G7）**：`spawn` 支持 `temperature` 覆写
  （`runtime.with_generation_overrides`）。
- 可选：主循环 `ToolContext` 注入 `workspace_sandbox` 以达 nanobot 两端一致。
