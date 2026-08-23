# Step113 架构设计：子代理 RequestContext 与 workspace_scope 绑定

## 1. 总体思路

沿用 step111/step112 已建立的子代理框架，**不改动** `runner.run` 的绑定机制，只在子代理侧把
「正确的请求上下文 / workspace 范围」作为 `AgentRunSpec` 字段传入，由 runner 完成实际的
`ContextVar` 绑定（对齐 nanobot：子代理在父会话上下文中执行）。

关键点：`runner.run` 已实现（`runner.py:280-286`）：
```python
req_ctx = spec.request_context or RequestContext(session_key=spec.session_key)
token = bind_request_context(req_ctx)
if spec.workspace_scope is not None:
    bind_workspace_scope(spec.workspace_scope)
```
因此本 step 只需保证 `spec.request_context` 与 `spec.workspace_scope` 被正确赋值即可，无需在
`subagent.py` 内手动 `bind/reset`。

## 2. 上下文来源与透传链路

```
父 turn（loop 已绑定 RequestContext + WorkspaceScope）
   │ SpawnTool.execute 捕获：
   │   req = current_request_context()         # channel/chat_id/session_key/message_id/runtime
   │   ws  = current_workspace_scope()          # 父 turn 的 WorkspaceScope
   ▼
manager.spawn(task, label, origin={
    "channel", "chat_id", "session_key",
    "message_id", "runtime", "workspace_scope"})
   ▼
SubagentManager._run_subagent:
   req_ctx = RequestContext(channel=..., chat_id=..., session_key=..., message_id=..., runtime=...)
   ws_scope = origin["workspace_scope"]
   AgentRunSpec(request_context=req_ctx, workspace_scope=ws_scope, ...)
   ▼
runner.run 自动 bind_request_context / bind_workspace_scope
   ▼
子代理工具内 current_request_session_key() / current_workspace_scope() 正确可用
```

## 3. 模块改动清单

### 3.1 `tools/spawn.py`（捕获并透传 origin）

- `execute()` 中：除 `session_key` 外，额外捕获 `req.channel / chat_id / message_id / runtime`
  与 `current_workspace_scope()`，组装为 `origin` 字典，调用
  `self._manager.spawn(task=task, label=label, origin=origin)`。
- 新增导入：`from step113.security.workspace_access import current_workspace_scope`。
- `current_request_context()` 在工具调用期由 loop 绑定，可得完整 `RequestContext`。

### 3.2 `subagent.py` —— `spawn` 签名兼容扩展

- 新增关键字形参 `origin: dict | None = None`；与既有 `origin_channel / origin_chat_id /
  session_key` 形参**向后兼容**：以 `origin` 字典优先，`setdefault` 回退到旧形参。
- 生效 origin 合并逻辑：
  ```python
  merged = dict(origin or {})
  merged.setdefault("channel", origin_channel)
  merged.setdefault("chat_id", origin_chat_id)
  merged.setdefault("session_key", session_key)
  origin = merged
  session_key = origin.get("session_key")  # 供 _session_tasks 使用
  ```
- 既有调用（`spawn(task=...)` / `spawn(task=..., session_key="s1")`）无需修改即可工作。

### 3.3 `subagent.py` —— `_run_subagent` 填充 spec 字段

- 新增导入：`from step113.context import RequestContext`。
- 在 `runner.run` 之前，由 `origin` 构建 `RequestContext` 与 `ws_scope = origin.get("workspace_scope")`，
  填入 `AgentRunSpec(request_context=req_ctx, workspace_scope=ws_scope)`。
- 其余字段（`initial_messages / tools / provider / max_iterations / hook`）不变。
- `_announce` 的路由仍使用 `origin["channel/chat_id/session_key"]`，不受新增 `workspace_scope` 键影响。
- 同步修正 `_build_tools` 文档中「共 11 个」为「共 13 个」并补列 `run_cli_app` / `list_exec_sessions`
  （属既有文档漂移修正，非功能改动）。

## 4. 数据流与边界

- `RequestContext` 字段映射：
  - `channel` ← `origin["channel"]`（默认 `"cli"`）
  - `chat_id` ← `origin["chat_id"]`（默认 `"direct"`）
  - `session_key` ← `origin["session_key"]`（可 `None`）
  - `message_id` ← `origin["message_id"]`
  - `runtime` ← `origin["runtime"]`
- `workspace_scope`：父 turn 的 `WorkspaceScope`（step112 `security/workspace_access.py`），
  由 `current_workspace_scope()` 取得；为 `None` 时 runner 不绑定（与 step112 行为一致）。
- 子代理本身不创建独立 session：结果仍以 `InboundMessage` 回注父会话（机制不变）。

## 5. 利弊与风险

- 利：结构性对齐 nanobot；为后续 `owner_session_key` 隔离铺路；修正子代理内
  `current_request_session_key()` / `current_workspace_scope()` 失效问题。
- 风险/注意：
  - 绑定 `workspace_scope` 后，子代理内 `current_tool_workspace()` 将返回 scope 的
    `project_path`（应与 `ToolContext.workspace` 一致），行为更贴近 nanobot；若 step112 的
    `ToolContext.workspace` 与父 scope 不一致会有差异——但二者均源自同一 workspace，预期一致。
  - 不改动 `exec_session.py` 的 owner 逻辑，故 `session_key` 绑定在当前版本无即时归属效果，
    属「铺路」性质（符合最小增量与边界约定）。

## 6. 不在本 step 范围

- `owner_session_key` 隔离（exec 会话归属 / 列表过滤）—— 后续 step。
- `cli_app_manager` 接线 —— 后续 step（step112.md §7）。
- 子代理 system prompt 模板化、子代理流式进度。
