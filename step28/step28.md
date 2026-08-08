# Step 28 — 运行时上下文 + 工作区绑定（A9/A10）

在 step 24（runner）/ 25（config）/ 26（事件层）/ 27（skills）之上，对齐
nanobot 两段链路：

- **A9 运行时上下文**：每 turn 动态解析的"代码上下文"（goal 状态、时钟等），
  以标记块追加到当前 user 消息，且不污染会话历史 / replay；
- **A10 workspace 绑定**：把 nanobot `security/workspace_access.py` 的
  workspace scope 模型落地（生产端），并让工具真正消费它（消费端）：
  `ReadFileTool` 受限读取 + runner turn 内 ContextVar 绑定。

---

## 一、这一阶段解决了什么问题、为什么要这样做

**A9**：此前 system prompt 是静态的——同一段 prompt 无论 turn 处于什么
状态都原样注入，agent 无法感知"当前时间/当前目标/当前资源状态"这类动态
信息。nanobot 的做法：`runtime_context.py` 让每个 provider
（`async (request) -> block(s) | None`）在 turn 边界串行解析一次，把
metadata-only 的上下文块以固定标记对追加到当前 user 消息；marker 可
精确移除，供历史展示期隐藏（lean 暂不持久化 marker，直接拼进内存的
initial_messages）。

### A10（消费端）
- 生产端已经解析出 `WorkspaceScope`（项目根 + restricted/full 模式），但
  工具侧无人消费——任何工具都能读任意路径；
- nanobot 的做法：`current_tool_workspace()` 提供工具查询入口，工具以
  workspace + restrict 意图勾出一致行为（read_file 受限时只读 workspace
  与内置技能目录，越界抛 `WorkspaceBoundaryError`）；
- runner 把 turn 级 `request_context` / `workspace_scope` 绑定进
  ContextVar（生命周期 = 单次 run），使"工具执行中能知道自己在哪个对话、
  哪个工作区"成为可测试的运行时事实。

## 二、目标与实现

| 目标 | 实现 |
|------|------|
| 运行时上下文模型 | `runtime_context.py`：`RuntimeContextBlock` / `RuntimeContextProvider` / `resolve_runtime_context`（串行、保持 provider 顺序、剥空白/滤空块）/ `append_runtime_context`（文本与多模态 list 两种形态 + 可移除 marker，lean 不持久化 marker）|
| 标记对包装 | `wrap_runtime_context_lines`：strip 后非空的行包进 `[Runtime Context — metadata only, not instructions]` / `[/Runtime Context]` |
| loop 集成 | `loop.py`：`register_runtime_context_provider`（去重）、`_resolve_runtime_context_for_turn`（透明 provider + 工具自带 provider 双源）、`_build_turn_request_context`、`_state_build` 只把块拼进**内存** initial_messages |
| workspace 生产端 | `security/workspace_access.py`：`WorkspaceScope`（project_path/access_mode/restrict_to_workspace/sandbox_status/source_channel）、`WorkspaceScopeResolver`（default()/for_message/for_turn/persist_message_scope）、`validate_workspace_scope_payload` / `workspace_scope_from_metadata` / `resolve_effective_workspace_scope`（消息 metadata 优先、会话 metadata 兜底）、sandbox 三级状态（off / system / application，`NANOBOT_WORKSPACE_SANDBOX_ENFORCED`/`_PROVIDER` 两个 env 探测） |
| 工具查询入口 | `current_tool_workspace(default_workspace, restrict_to_workspace=False, sandbox_restricts_workspace=False)` → `ToolWorkspace`（含 `allowed_root` 派生属性）；ContextVar 绑定 `/`重置 `bind_workspace_scope` `/` reset_workspace_scope`、读取 `current_workspace_scope()`；loopback 门禁 `current_scope_allows_loopback` |
| 工具消费端 | `tools/read_file.py`：`ReadFileTool.create(ctx)` 收 `ToolContext(workspace, restrict_to_workspace)` 装配真实意图；`execute` 内 `current_tool_workspace` 解析策略 → `resolve_allowed_path(path, workspace=, allowed_root=, extra_allowed_roots=[BUILTIN_SKILLS_DIR])`，越界抛 `WorkspaceBoundaryError` → `ToolResult.error`；`max_chars` 截断提示 |
| runner 绑定 | `runner.py`：`AgentRunSpec.workspace_scope` / `request_context` 字段；run 时注入 ContextVar（`current_workspace_scope` / `current_request_context`），finally 恢复（验证无残留） |
| loop 装配 | `loop.py`：`restrict_to_workspace` → `WorkspaceScopes`；`_build_agent_spec` 写入 `workspace_scope`（`default()`）+ `request_context`（turn_id/session_key/channel/workspace），ToolLoader 以真实 scope 装载工具到 registry |
| config 贯通 | `config/schema.py`：`tools.restrict_to_workspace`；`loop.py:from_config` 透传；`main.py` 同步 |
| 测试 | `tests/test_runtime_context.py`（块构造/包装/规范化/串行解析/loop 集成）、`tests/test_workspace_tool.py`（ToolContext 真值、边界强制 9 种、runner 绑定、loop 装配），全构造数据 + 脚本化 provider；无真实 API |

## 三、核心函数 / 类说明

### `security/workspace_access.py`（生产端）
- `WorkspaceScope`：不可变 turn 快照；错误请求抛 `WorkspaceScopeError(400)`。
- `WorkspaceScopeResolver(default_workspace, default_restrict_to_workspace, scoped_channel="websocket")`：非 scoped 通道一律 `default()`，scoped 通道走 metadata 覆盖（对齐 nanobot WebUI 语义）。
- `default_workspace_scope(workspace, restrict)` / `build_workspace_scope`：路径规范化 + sandbox 状态解析。
- `current_tool_workspace(...)`：**工具侧唯一查询入口**——`scope is not None` 时以绑定为准，否则回退构造参数。

### `tools/read_file.py`（消费端）
- `ReadFileTool.create(ctx)` / `__init__(workspace="", restrict_to_workspace=False)`：构造期权限意图（ContextVar 无绑定时回退用）。
- `execute(path, max_chars)`：空 path → error；`resolve_allowed_path`（受限 → 仅 `allowed_root` + 内置技能目录豁免；未受限 → 相对路径按 workspace 解析、绝对路径直通）；越界/非法路径 → `WorkspaceBoundaryError` 文案；文件缺失/目录 → 明确 error；超长 → `...[truncated]`。

### `runner.py`（绑定）
- `AgentRunSpec.workspace_scope` / `request_context`：可选的 turn 绑定字段；run 前 `bind_workspace_scope` + `current_request_context` set，finally 恢复。

### `loop.py`
- `WorkspaceScopes`（聚合）：`default()` 返回 scope；`restrict_to_workspace` 构造参数 + `from_config(config.tools.restrict_to_workspace)` 贯通。
- `_build_agent_spec`：组装 spec 的 workspace_scope（`default()`）与 `request_context`（`_build_turn_request_context`）；ToolLoader 用 `ToolContext(workspace=scope.project_path, restrict_to_workspace=scope.restrict_to_workspace)` 装载 read_file 等（`loop.registry.get("read_file")` 可断言真值）。

### `runtime_context.py`（A9）
- `RuntimeContextBlock(source, content)`；`resolve_runtime_context` 串行执行 providers（含工具类 provider 聚合进 loop）。
- `append_runtime_context(content, blocks)`：多模态 list → 追加 text 块；文本 → 拼接；无块 → 原样 + marker=None。
- `wrap_runtime_context_lines`：strip 后非空行包进标记对（空输入返回 ""）。

## 四、暴露的问题 / 取舍

| 取舍 | 说明 | 后续计划 |
|------|------|----------|
| 复用 ContextVar 与并发工具 | runner 的异步工具在同一 context 内执行，ContextVar 共享同一 turn 的 scope（对齐 asyncio 语义）；若未来并发工具需显式隔离再按需处理 | 观察即可 |
| marker 不持久化 | A9 只把块合并进内存 initial_messages，session 历史保留原文本（避免污染 replay/consolidation）；nanobot 的 `public_history_message(s)` 展示期移除未落地 | step29 历史可见性（session/history_visibility.py，HIDDEN_HISTORY_META）时评估对齐 |
| 无内置 runtime provider | clock 等演示 provider 由 main.py 注册 | 产品侧自行注册 |
| workspace 受限时 skills 目录豁免 | 对齐 nanobot `extra_read` 只读内置技能；若未来内置技能由用户 workspace 管理，需收紧 | 视产品需要 |
| 默认可回退 `restrict_to_workspace=False` | 与 nanobot 默认一致（不默认锁死用户路径） | 产品决定默认 |
| sandbox 探测只认两个 env | 不做 macOS/bwrap 平台自动探测 | 视产品需要扩展 |

## 五、下一 step 要解决什么

会话记忆与历史可见性（`session/history_visibility.py`、HIDDEN_HISTORY_META、
`public_history_message(s)` 展示期移除 runtime marker），让 A9 的 marker
语义真正可用并对齐 nanobot 的「隐藏历史 + 会话记忆」链路。