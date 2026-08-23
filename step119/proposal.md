# Step119 需求定义：self/my 工具可观测子代理状态

## 1. 问题背景

step118 完成子代理 microcompaction 工具集对齐。但父代理**无法查询运行中的子代理状态**——
`SubagentManager._task_statuses`（`subagent.py:251`）虽有所有任务状态（task_id/label/phase/iteration/...），
却没有对外暴露的公开方法，且 `self.py` 的 `MyTool`（工具名 `my`）未提供任何子代理可观测 key。
nanobot 已通过 `my get subagents` 暴露 `SubagentManager._task_statuses`（对齐 `self.py`）。

## 2. 本 step 要解决什么

让 `my` 工具新增只读 key `subagents`，返回当前所有子代理任务状态快照，使父代理可观测运行中的子代理，
对齐 nanobot `agent/tools/self.py` 的 `subagents` 可观测项。

## 3. 为什么这样做（方案取舍）

- 复用既有 `ToolContext.subagent_manager`（`context.py:106`，已为 spawn 工具注入），无需新增接线。
- `SubagentManager` 新增公开方法 `get_task_statuses()`（用 `dataclasses.asdict` 转为可序列化 dict 列表），
  工具层只调此方法，不触碰 `_task_statuses` 内部。
- 格式沿用本仓库 `my` 工具统一的结构化 JSON（非 nanobot 的文本摘要），降低风格不一致。

## 4. 目标与实现边界（最小增量）

- 目标：`my get subagents` 返回子代理状态列表（含 task_id/label/phase/iteration 等）；无 manager 时返回 `[]`。
- 边界（**不做**）：
  - 不改变 `set subagents`（read-only，被既有安全边界拒绝）；
  - 不支持嵌套路径 `subagents.<id>`（保持最小增量）；
  - 不改动 `SubagentManager` 既有字段语义。

## 5. 验收标准

1. `SubagentManager.get_task_statuses()` 返回 `list[dict]`，元素为 `SubagentStatus` 的 `asdict`。
2. `MyTool` 支持 `get subagents`：返回 `ctx.subagent_manager.get_task_statuses()`；无 manager 时返回 `[]`。
3. `set subagents` 被既有安全边界拒绝。
4. 新增测试全绿；全量失败数与 step118 基线（25）持平，无新增回归。
