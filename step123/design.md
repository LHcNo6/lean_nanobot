# step123 架构设计：子代理 ToolContext 沙箱 + 多相位状态（G9 + G10）

## 1. 总体设计

两个独立、正交的增强，均在 `SubagentManager`（step123/subagent.py）侧完成：

- **G9（F1 + F2）**：扩展 `ToolContext`（step123/context.py）加 `workspace_sandbox` 字段；
  子代理 `_build_tools` 在构造 `ToolContext` 时调用既有
  `workspace_sandbox_status(restrict_to_workspace, workspace)` 填充该字段。
- **G10（F3）**：`_run_subagent` 内定义 `_on_checkpoint(payload)` 闭包，把 runner 在迭代中
  发出的 checkpoint `phase`/`iteration` 同步到 `SubagentStatus`，并将闭包作为
  `checkpoint_callback` 传入 `AgentRunSpec`。

核心原则：**最小增量 + 复用既有能力**。G9 复用既有 `workspace_sandbox_status`；G10 复用
runner 已实现的 checkpoint 机制（无需改 runner）。

## 2. 关键原理

### 2.1 G9：ToolContext 字段与沙箱状态

- `ToolContext`（step123/context.py:87）是工具装配上下文 dataclass；加一个可选字段
  `workspace_sandbox: Any | None = None` 不影响任何既有构造（默认 `None`）。
- `workspace_sandbox_status`（security/workspace_access.py:212）签名：
  `(*, restrict_to_workspace: bool, workspace: str | Path, environ=None) -> WorkspaceSandboxStatus`。
  三级状态：`off`（未限制）/ `system`（外部沙箱强制）/ `application`（应用级守卫）。
- 子代理 `_build_tools`（step123/subagent.py:359）当前构造 `ToolContext` 时已有
  `restrict_to_workspace=self._restrict_to_workspace` 与 `workspace=...`；只需额外传
  `workspace_sandbox=workspace_sandbox_status(restrict_to_workspace=self._restrict_to_workspace,
  workspace=root)`，其中 `root` 与 `workspace` 字段取同一值。

### 2.2 G10：runner 已发 checkpoint，仅需接线

- `AgentRunSpec.checkpoint_callback`（runner.py:117）：`Callable[[dict], Awaitable[None]] | None`。
- runner 在迭代循环里已通过 `_emit_checkpoint` 发出含 `phase`/`iteration` 的 payload：
  - `awaiting_tools`（runner.py:1176，LLM 返回工具调用后）
  - `tools_completed`（runner.py:1229，工具执行完后）
  - `final_response`（runner.py:1335，最终响应写出前）
  - `after max_iterations`（runner.py:1365，触顶边界）
- `_emit_checkpoint`（runner.py:906）在 `checkpoint_callback` 非空时调用 `await callback(payload)`。
- 因此 G10 只需要在 `_run_subagent` 内：定义 `async def _on_checkpoint(payload):`
  `status.phase = payload.get("phase", status.phase)`（并同步 `iteration`），
  再 `AgentRunSpec(..., checkpoint_callback=_on_checkpoint)`。runner 会自动在每轮迭代调用它，
  驱动 `status.phase` 在 `initializing → awaiting_tools → tools_completed → final_response → done`
  之间流转（异常路径置 `error`）。

## 3. 改动文件清单

- `step123/context.py`：`ToolContext` 新增 `workspace_sandbox: Any | None = None` 字段。
- `step123/subagent.py`：
  - 导入 `workspace_sandbox_status`；`_build_tools` 注入 `workspace_sandbox`。
  - `_run_subagent` 增加 `_on_checkpoint` 闭包并传给 `AgentRunSpec`。
- 新增 `step123/tests/test_subagent_sandbox_phase.py`：G9（spy ToolContext）+ G10（真实 runner 相位记录）。
- 不改动 `runner.py` / `loop.py`。

## 4. 测试策略

- **G9**：`monkeypatch("step123.subagent.ToolContext", _SpyToolContext)`（子类记录构造 kwargs），
  调用 `mgr._build_tools()`，断言 `kwargs["workspace_sandbox"]` 为 `WorkspaceSandboxStatus`
  实例且非 `None`，且其 `restrict_to_workspace` 与 `self._restrict_to_workspace` 一致。
- **G10**：用真实 `runner.run` + mock `LLMProvider`（首轮返回 `list_exec_sessions` 工具调用、
  次轮返回 stop 文本）；在 `provider.chat` 内读取 `mgr.get_task_statuses()[0]["phase"]` 入列表；
  断言运行期间出现非终态相位（如 `tools_completed`），证明 checkpoint 成功驱动多相位。

## 5. 验收标准

- 全量 `step123/tests` 失败数与 step122 基线（25）持平。
- 新增用例全绿。
- 配套 `step123.md` + 三份规范齐备，提交并推送。
