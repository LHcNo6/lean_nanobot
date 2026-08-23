# Step113 配套文档：子代理 RequestContext 与 workspace_scope 绑定

## 1. 问题背景

step112 已把子代理工具集补齐到 13 个，与 nanobot 对齐。但子代理的**运行上下文**仍缺一块：

- nanobot 的 `SubagentManager._run_subagent` 会 `bind_request_context(RequestContext(
  channel, chat_id, message_id, session_key, runtime))` 并 `bind_workspace_scope(workspace_scope)`，
  让子代理在「父会话上下文」中执行；
- step112 的 `_run_subagent` 构造 `AgentRunSpec` 时**未传** `request_context` / `workspace_scope` /
  `session_key`，于是 `runner.run` 回退为 `RequestContext(session_key=None)`，子代理内
  `current_request_session_key()` 与 `current_workspace_scope()` 全部失效。

这不仅是「上下文缺失」，更是后续 `owner_session_key` 会话隔离（nanobot 用父 session_key 作为
owner）的前置地基——不先绑定正确 session_key，隔离会直接错乱。

## 2. 本 step 解决什么 & 为什么这样做

**目标**：让子代理运行期拥有与父会话一致的 `RequestContext` 与 `WorkspaceScope`，对齐 nanobot。

**为什么（方案取舍）**：
- 方案 A「在 `_run_subagent` 内手动 bind/reset ContextVar」：重复 `runner.run` 已有逻辑且易漏
  `reset` 导致泄漏 → **否决**。
- 方案 B（选定）「把正确的 `request_context` / `workspace_scope` 填进 `AgentRunSpec`」：复用
  runner 的绑定（`runner.py:280-286`），改动最小、与 nanobot 对齐。

**上下文来源**：`SpawnTool.execute` 在父 turn 内捕获完整 `RequestContext` 与
`current_workspace_scope()`，经 `spawn(origin=...)` 透传给子代理——与 nanobot 从 spawn 调用点
携带 origin 的设计一致。

## 3. 原理思路与具体实现

复用既有框架、只填字段，不重复绑定逻辑：

1. **`tools/spawn.py`**：`execute()` 组装 `origin` 字典（channel/chat_id/session_key/message_id/
   runtime/workspace_scope），调用 `manager.spawn(task, label, origin=origin)`。新增导入
   `current_workspace_scope`。
2. **`subagent.py` `spawn`**：新增 `*` 形参 `origin: dict | None = None`，与旧形参
   `origin_channel/origin_chat_id/session_key` **向后兼容**——以 `origin` 字典优先，
   `setdefault` 回退旧形参（`merged = dict(origin or {}); merged.setdefault(...)`）。
3. **`subagent.py` `_run_subagent`**：由 `origin` 重建 `RequestContext`（channel/chat_id/
   session_key/message_id/runtime）与 `ws_scope = origin.get("workspace_scope")`，填入
   `AgentRunSpec(request_context=req_ctx, workspace_scope=ws_scope)`。`runner.run` 据此自动
   完成 `bind_request_context` / `bind_workspace_scope`，子代理内上下文查询即生效。
4. `_announce` 的回传路由仍用 `origin` 的 channel/chat_id/session_key，不受新增键影响；
   顺手修正 `_build_tools` 文档「11 个」→「13 个」。

## 4. 核心函数 / 类功能说明

| 符号 | 位置 | 功能 |
| --- | --- | --- |
| `SpawnTool.execute` | tools/spawn.py | 捕获父 turn 请求上下文 + workspace 范围，透传 `origin` |
| `SubagentManager.spawn` | subagent.py | 合并 `origin` 与旧形参（向后兼容），奠定子代理上下文来源 |
| `SubagentManager._run_subagent` | subagent.py | 由 `origin` 重建 `RequestContext`/`workspace_scope` 注入 `AgentRunSpec` |
| `runner.run` | runner.py | 已有逻辑：`bind_request_context(spec.request_context)` + `bind_workspace_scope(spec.workspace_scope)` |

## 5. 验证结果

- 新增 `tests/test_subagent_tool_isolation.py::TestSubagentRequestContextBinding`：
  - `test_spawn_binds_origin_context_to_spec`：origin 透传为 `spec.request_context`（session_key/
    channel/chat_id/message_id 正确）与 `spec.workspace_scope`（同一对象）；
  - `test_default_origin_falls_back`：缺省 origin 时 `session_key=None` / `workspace_scope=None`（无回归），
    且旧式 `spawn(task=..., session_key="legacy")` 仍向后兼容。
- 该测试文件 13 项全绿；step113 全量 `tests`：**25 failed / 1137 passed**，与 step112 基线
  （25 failed）相比失败集合完全一致、**无新增回归**（通过的 +2 即本 step 新增两项测试）。
- 全程 mock provider / 构造数据，未触发真实网络或 API。

## 6. 暴露了什么问题

1. **`owner_session_key` 隔离尚未实现**：本 step 仅绑定了正确的 `session_key`，但
   `exec_session.py` 仍是 step112 简化的「无 owner 隔离」版本，子代理创建的会话不归属父会话。
   本 step 属「铺路」，下一步才是真正的隔离。
2. **`ToolContext.workspace` 与 `WorkspaceScope.project_path` 一致性依赖**：绑定 scope 后，
   子代理内 `current_tool_workspace()` 改用 scope 的 `project_path`，须与 `_build_tools` 传入的
   `ToolContext.workspace` 保持同源（二者均来自同一 workspace，预期一致）。

## 7. 下一 step 要解决什么

- 实现 `exec_session.py` 的 `owner_session_key` 隔离：子代理/父代理创建的会话按
  `current_request_session_key()` 归属，列表与读写按 owner 过滤（对齐 nanobot
  `exec_session.py` 的 `owner_session_key` 机制）；本 step 已把正确的 session_key 注入，可直接复用。
- （可选）评估 `cli_app_manager` 接线，使 step112 同步的 `run_cli_app` 在子代理/主代理真正可用
  （见 step112.md §7）。
