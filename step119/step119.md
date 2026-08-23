# Step119：self/my 工具可观测子代理状态

## 1. 问题背景

step118 完成子代理 microcompaction 工具集对齐。但父代理**无法查询运行中的子代理状态**——
`SubagentManager._task_statuses`（`subagent.py:251`）虽有所有任务状态（task_id/label/phase/iteration/...），
却无对外暴露的公开方法，且 `self.py` 的 `MyTool`（工具名 `my`）未提供任何子代理可观测 key。
nanobot 已通过 `my get subagents` 暴露 `SubagentManager._task_statuses`（对齐 `self.py`）。

## 2. 这一 step 解决了什么 / 为什么这样做

让 `my` 工具新增只读 key `subagents`，返回当前所有子代理任务状态快照，使父代理可观测运行中的子代理，
对齐 nanobot `agent/tools/self.py` 的 `subagents` 可观测项。

方案取舍：
- 复用既有 `ToolContext.subagent_manager`（`context.py:106`，已为 spawn 工具注入），无需新增接线。
- `SubagentManager` 新增公开方法 `get_task_statuses()`（用 `dataclasses.asdict` 转为可序列化 dict 列表），
  工具层只调此方法，不触碰 `_task_statuses` 内部。
- 格式沿用本仓库 `my` 工具统一的结构化 JSON（非 nanobot 文本摘要），降低风格不一致。

## 3. 原理思路与具体实现

### 3.1 SubagentManager.get_task_statuses（subagent.py）
```python
from dataclasses import asdict
def get_task_statuses(self) -> list[dict[str, Any]]:
    """返回所有子代理任务状态的快照（step119，对齐 nanobot self/my 可观测）。"""
    return [asdict(status) for status in self._task_statuses.values()]
```
- 用 `asdict` 把 `SubagentStatus` 转为纯 dict（tool_events/usage 等一并展开）；只读，不暴露内部引用。

### 3.2 MyTool 新增 subagents key（self.py）
- `_READ_ONLY` 增加 `"subagents"`（明确不可 set）；`_has_key` 的 `known` / `known_tops` 集合增加 `"subagents"`。
- `_get_runtime_value` 单层分支增加：
  ```python
  if key == "subagents":
      mgr = getattr(ctx, "subagent_manager", None)
      if mgr is None or not hasattr(mgr, "get_task_statuses"):
          return []
      return mgr.get_task_statuses()
  ```
- `set subagents` 经 `_READ_ONLY` / `allowed_settable` 拒绝。

### 3.3 接线（零改动）
`ToolContext.subagent_manager` 已由 loop 注入（供 spawn 使用），`MyTool.create(ctx)` 已保存 `self._ctx`，
运行时自动可见。

## 4. 核心函数 / 类功能说明

| 元素 | 职责 |
| --- | --- |
| `SubagentManager.get_task_statuses()` | 返回子代理状态快照 list[dict]（asdict） |
| `MyTool` 的 `subagents` key | `get` 返回状态列表；`set` 被拒（read-only） |

## 5. 暴露了什么问题 / 后续

- 暴露：返回全部状态（含已结束）；调用方可按 `phase` 过滤。若需「仅运行中」可后续加过滤参数。
- 暴露：未支持嵌套路径 `subagents.<id>`（nanobot 支持），本 step 保持最小增量，未做。
- 子代理对齐 nanobot 路线（step110–119）至此**核心项全部完成**：编排 / 工具集隔离 / 运行上下文 /
  会话隔离 / 应用目录 / prompt 模板化 / 运行时限制 / 流程压缩 / 可观测。
  高级特性（announce body 通道清洗 `subagent_channel_display`、退出自动取消等）属打磨，不在核心范围。

## 6. 验证

- 新增 `tests/test_subagent_status_observability.py`：5 个用例全绿。
  - `get_task_statuses` 返回等长 list[dict]，含 task_id/phase/iteration；空时 `[]`；
  - `my get subagents` JSON 含任务 task_id/phase；manager=None 时返回 `[]`；
  - `my set subagents` 被 read-only 边界拒绝。
- 全量 `step119/tests`：**25 failed / 1169 passed**（与 step118 基线 25 持平，新增 5 通过，无新增回归）。
  失败用例为 Windows 既有问题，与子代理状态可观测无关。
