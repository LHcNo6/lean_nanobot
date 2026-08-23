# Step119 接口契约（api-spec）

本文件定义 step119「self/my 工具可观测子代理状态」的对外契约。

## D1：SubagentManager.get_task_statuses

```python
def get_task_statuses(self) -> list[dict[str, Any]]: ...
```

契约：
- 返回 `self._task_statuses.values()` 的 `dataclasses.asdict` 列表；
- 空时返回 `[]`；元素键含 `task_id/label/task_description/phase/iteration/tool_events/usage/stop_reason/error`。

## D2：MyTool 支持 get subagents

```python
# execute(action="get", key="subagents")
```

契约：
- `ctx.subagent_manager` 存在且有 `get_task_statuses` → 返回该方法结果（list[dict]）；
- `ctx.subagent_manager` 为 None 或不具备该方法 → 返回 `[]`；
- 返回经 `_safe_repr` + `json.dumps` 的结构化 JSON 字符串。

## D3：set subagents 被拒

- `action="set", key="subagents"` 应被拒绝（read-only / 不在 `allowed_settable`），返回错误 ToolResult。

## D4：测试映射

| 契约 | 测试 |
| --- | --- |
| D1 | `get_task_statuses` 放回等长 list[dict]，含 task_id/phase |
| D2 | `my get subagents` JSON 含任务 task_id/phase；manager=None 时返回 `[]` |
| D3 | `my set subagents` 返回 read-only/不可设置错误 |

> 全部测试使用构造数据 / 假管理器，禁止真实网络与 API 调用。
