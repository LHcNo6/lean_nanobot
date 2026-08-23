# Step119 架构设计：self/my 工具可观测子代理状态

## 1. 总体思路

父代理经 `my get subagents` 查询子代理状态。链路：
- `MyTool` 在 `ToolContext` 上取到 `subagent_manager`（已为 spawn 注入）；
- 调用新增公开方法 `SubagentManager.get_task_statuses()` 拿到可序列化状态列表；
- 经既有 `_safe_repr` + `json.dumps` 返回结构化 JSON。

## 2. 改动点

### 2.1 SubagentManager.get_task_statuses（subagent.py）
```python
from dataclasses import asdict
def get_task_statuses(self) -> list[dict[str, Any]]:
    """返回所有子代理任务状态快照（供 self/my 工具可观测，对齐 nanobot）。"""
    return [asdict(st) for st in self._task_statuses.values()]
```
- 用 `asdict` 把 `SubagentStatus` 转为纯 dict（tool_events/usage 等嵌套结构一并展开）；
- 只读，不暴露管理器内部引用。

### 2.2 MyTool 新增 subagents key（self.py）
- `_READ_ONLY` 增加 `"subagents"`（明确不可 set）；
- `_has_key` 的 `known` / `known_tops` 集合增加 `"subagents"`；
- `_get_runtime_value` 单层分支增加：
  ```python
  if key == "subagents":
      mgr = getattr(ctx, "subagent_manager", None)
      if mgr is None or not hasattr(mgr, "get_task_statuses"):
          return []
      return mgr.get_task_statuses()
  ```
- `set subagents` 经 `_READ_ONLY` / `allowed_settable` 拒绝。

## 3. 数据流

```
父代理 my get subagents
  └─ MyTool._get_runtime_value("subagents")
       └─ ctx.subagent_manager.get_task_statuses()
            └─ [asdict(st) for st in SubagentManager._task_statuses.values()]
       └─ _safe_repr → json.dumps → 结构化 JSON 返回
```

## 4. 利弊与风险

- 利：父代理可观测子代理（running/done/error），对齐 nanobot；改动极小、零新依赖、零接线。
- 风险/注意：
  - 返回全部状态（含已结束），调用方可按 `phase` 过滤；如需仅运行中可后续加过滤参数。
  - 沿用 JSON 而非 nanobot 文本摘要，风格统一但信息形态不同（不影响功能）。

## 5. 不在本 step 范围

- `subagents.<id>` 嵌套查询（按 id 取单条）；
- 文本摘要格式化（nanobot `_format_status`）；
- `self`（若有独立工具）同步——本仓库 `self.py` 仅含 `MyTool`，已覆盖。

## 6. 下一 step

子代理对齐 nanobot 路线（step110–119）至此完成。后续可打磨：announce body 通道清洗
`subagent_channel_display`、退出自动取消等高级特性（属路线图「高级特性」外，非核心范围）。
