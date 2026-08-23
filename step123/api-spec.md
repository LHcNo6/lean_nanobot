# step123 接口契约（api-spec）

> 本文件定义 step123「子代理 ToolContext 沙箱 + 多相位状态（G9 + G10）」的对外契约。
> 改动范围：`context.py`（`ToolContext` 字段）、`subagent.py`（`_build_tools` 注入 + `checkpoint_callback`）。

## A. ToolContext 字段契约（G9 / F1）

`step123/context.py` 的 `ToolContext` 新增字段：

| 字段 | 类型 | 缺省 | 含义 |
| --- | --- | --- | --- |
| `workspace_sandbox` | `Any \| None` | `None` | 宿主 workspace 限制状态（`WorkspaceSandboxStatus`），由子代理装配时填充 |

- 主循环 `loop.py:1251` 构造 `ToolContext` 不改（保持 `None`），仅子代理填充（见 B）。

## B. 子代理 ToolContext 注入契约（G9 / F2）

`SubagentManager._build_tools`（step123/subagent.py）构造 `ToolContext` 时，在既有参数之外
新增：

```python
tool_ctx = ToolContext(
    ...,
    workspace_sandbox=workspace_sandbox_status(
        restrict_to_workspace=self._restrict_to_workspace,
        workspace=root,  # 与既有 workspace 字段同源
    ),
)
```

其中 `workspace_sandbox_status` 来自 `step123.security.workspace_access`；返回
`WorkspaceSandboxStatus`（`restrict_to_workspace` / `workspace_root` / `level` /
`enforced` / `provider` / `summary`）。

## C. checkpoint_callback 契约（G10 / F3）

`SubagentManager._run_subagent` 内定义闭包并传入 `AgentRunSpec`：

```python
async def _on_checkpoint(payload: dict) -> None:
    status.phase = payload.get("phase", status.phase)
    status.iteration = payload.get("iteration", status.iteration)

await self.runner.run(AgentRunSpec(
    ...,
    checkpoint_callback=_on_checkpoint,
))
```

- `payload` 由 runner 提供，含 `phase`（str）、`iteration`（int）等键（runner.py:1176/1229/1335/1365）。
- `_on_checkpoint` 仅做「有则更新」的防御式同步，缺失键时保留原值。
- 不变量：`status.phase` 终态仍由 `_run_subagent` 在成功/异常分支置 `done`/`error`（不变）；
  运行期相位随 runner 迭代从 `initializing` 流转至 `awaiting_tools`/`tools_completed`/
  `final_response`（或 `after max_iterations`）。

## D. 不变量

- `ToolContext.workspace_sandbox` 缺省 `None`，既有任何 `ToolContext(...)` 构造（含主循环/
  各测试）无需改动即兼容。
- `AgentRunSpec.checkpoint_callback` 为 `None` 时 runner 不触发（既有行为不变）；step123 仅
  在子代理路径置该回调。
- 不改 `runner.py` 与 `loop.py`；不引入新的运行期限制语义。
