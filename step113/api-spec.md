# Step113 接口契约（api-spec）

本文件定义 step113「子代理 RequestContext / workspace_scope 绑定」的对外契约，供实现与测试对齐。

## B1：spawn 调用契约（向后兼容）

`SubagentManager.spawn` 签名新增关键字形参 `origin`:

```python
async def spawn(
    self,
    task: str,
    label: str | None = None,
    origin_channel: str = "cli",
    origin_chat_id: str = "direct",
    session_key: str | None = None,
    *,
    origin: dict | None = None,
) -> str
```

- `origin` 为 `dict`，可含键：`channel / chat_id / session_key / message_id / runtime / workspace_scope`。
- 合并规则：以 `origin` 优先，`setdefault` 回退到旧形参 `origin_channel / origin_chat_id / session_key`。
- 向后兼容：既有 `spawn(task=...)`、`spawn(task=..., session_key="s1")` 行为不变。
- `SpawnTool.execute` 现组装并透传：
  ```python
  origin = {
      "channel": req.channel, "chat_id": req.chat_id,
      "session_key": req.session_key, "message_id": req.message_id,
      "runtime": req.runtime, "workspace_scope": current_workspace_scope(),
  }
  ```

## B2：_run_subagent 的 AgentRunSpec 契约

`SubagentManager._run_subagent` 构造 `AgentRunSpec` 时必须包含：

```python
AgentRunSpec(
    ...
    request_context=RequestContext(
        channel=origin.get("channel") or "cli",
        chat_id=origin.get("chat_id") or "direct",
        session_key=origin.get("session_key"),
        message_id=origin.get("message_id"),
        runtime=origin.get("runtime"),
    ),
    workspace_scope=origin.get("workspace_scope"),
)
```

- `request_context` 为 `step113.context.RequestContext` 实例。
- `workspace_scope` 为 `step113.security.workspace_access.WorkspaceScope` 或 `None`。
- runner 据此自动 `bind_request_context` / `bind_workspace_scope`（`runner.py:280-286`）。

## B3：回退契约（无 origin 时不回归）

当 `origin` 为 `None` / 缺键时：

- `session_key` → `None`；
- `workspace_scope` → `None`；
- `request_context` 字段缺失时回退默认（`channel="cli"`、`chat_id="direct"`）；
- 子代理运行行为与 step112 完全一致（即 `current_request_session_key()` 为 `None`、
  `current_workspace_scope()` 为 `None`）。

## B4：RequestContext 字段说明

| 字段 | 来源 | 说明 |
| --- | --- | --- |
| `channel` | `origin["channel"]` | 父 turn 通道（默认 `"cli"`） |
| `chat_id` | `origin["chat_id"]` | 父 turn 聊天标识（默认 `"direct"`） |
| `session_key` | `origin["session_key"]` | 父会话 key（用于后续 owner 隔离） |
| `message_id` | `origin["message_id"]` | 触发消息 ID |
| `runtime` | `origin["runtime"]` | 父 turn 的 LLMRuntime |

## 测试映射

| 契约 | 测试 |
| --- | --- |
| B1 + B3 | `tests/test_subagent_tool_isolation.py::TestSubagentRequestContextBinding::test_default_origin_no_context`（回退） |
| B2 + B1 | `tests/test_subagent_tool_isolation.py::TestSubagentRequestContextBinding::test_spawn_binds_origin_context_to_spec`（绑定） |
| B2 | 断言 `spec.request_context.session_key == "s1"` 且 `spec.workspace_scope is <注入的 scope>` |

> 全部测试使用 mock provider / 构造数据，禁止真实网络与 API 调用。
