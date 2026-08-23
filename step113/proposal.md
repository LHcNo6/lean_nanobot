# Step113 需求定义：子代理 RequestContext 与 workspace_scope 绑定

## 1. 问题背景

step112 已把子代理工具集补齐到 13 个，与 nanobot 对齐。但子代理的**运行上下文**仍有一处
结构性缺口：

- nanobot 在 `SubagentManager._run_subagent` 中，会先 `bind_request_context(RequestContext(
  channel, chat_id, message_id, session_key, runtime))` 并 `bind_workspace_scope(workspace_scope)`，
  让子代理在「父会话的上下文」中执行；
- step112 的 `_run_subagent`（`step112/subagent.py:268`）构造 `AgentRunSpec` 时**没有传
  `request_context` / `workspace_scope` / `session_key`**，于是 `runner.run` 回退为
  `RequestContext(session_key=None)`（`runner.py:280`），子代理内部
  `current_request_session_key()` 为 `None`、`current_workspace_scope()` 为 `None`。

这一缺口导致：子代理工具拿不到正确的会话标识与 workspace 策略——虽然 step112 的
`ToolContext.workspace` 已设置，但 `current_workspace_scope()` / `current_request_session_key()`
这类基于 `RequestContext` 的查询在子代理内全部失效，且在未来引入 `owner_session_key`
会话隔离时（nanobot 用父 session_key 作为 owner）会直接导致隔离错乱。

## 2. 本 step 要解决什么

在子代理运行时不以「裸」上下文执行，而是**绑定与父会话一致的 `RequestContext` 与 `WorkspaceScope`**，
对齐 nanobot 的 `_run_subagent` 上下文绑定行为。

## 3. 为什么这样做（方案取舍）

- 方案 A「在 `_run_subagent` 内手动 bind/reset ContextVar」：可行，但重复 runner 已实现的
  绑定逻辑，且易遗漏 `reset` 导致上下文泄漏。**否决**。
- 方案 B（选定）「把正确的 `request_context` / `workspace_scope` 填进 `AgentRunSpec`，
  复用 `runner.run` 已有的绑定」：改动最小、与 nanobot 对齐、无重复逻辑。
  `runner.run` 已经会在运行期 `bind_request_context(spec.request_context)` 与
  `bind_workspace_scope(spec.workspace_scope)`（`runner.py:280-286`），本 step 只需把字段填对。
- 上下文来源：由 `SpawnTool.execute` 在父 turn 内捕获「当前 `RequestContext` + 当前
  `current_workspace_scope()`」，通过 `spawn(origin=...)` 透传给子代理。这与 nanobot 从
  spawn 调用点携带 origin 的设计一致。

## 4. 目标与实现边界（最小增量）

- 目标：子代理运行期 `current_request_session_key()` 返回父会话 key（`current_workspace_scope()`
  返回父 scope），与 nanobot 对齐。
- 边界（**不做**）：
  - 不实现 `owner_session_key` 会话隔离（仅铺路，本 step 不改 `exec_session.py` 的归属逻辑）；
  - 不接线 `cli_app_manager`（属另一最小增量，见 step112.md §7）；
  - 不改子代理 system prompt、不引入流式进度。

## 5. 验收标准

1. 经 `SpawnTool` 派生的子代理，其 `AgentRunSpec.request_context.session_key` 等于父会话
   `session_key`，且 `workspace_scope` 等于父 turn 的 `WorkspaceScope`。
2. 旧式调用（不带 `origin`）仍可用：`spawn(task=...)` 回退为 `session_key=None`、
   `workspace_scope=None`，行为与 step112 一致（无回归）。
3. 兼容既有 `spawn(task=..., session_key=...)` / `origin_channel/origin_chat_id` 形参。
4. 新增单元测试覆盖上述绑定与回退；step113 全量测试相对 step112 基线（25 failed）无新增回归。
