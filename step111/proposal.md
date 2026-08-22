# Step111 需求定义：Subagent 工具集隔离（scope="subagent"）

## 问题背景

对齐分析（step110 vs `nanobot/nanobot/agent/subagent.py`）发现的最大安全差距：
`SubagentManager` 直接复用主 agent 的 `ToolRegistry`（subagent.py:63，main.py:103 装配时
传入 `tools=registry`），导致子代理可以调用**全部核心工具**：

1. **递归 spawn 风险**：子代理可再调 `spawn` 生成子代理（仅受全局并发数兜底，
   无结构性防护），任务树可能失控；
2. **越权操作**：子代理可调用 `message`（主动向用户发消息）、`create_goal`/
   `update_goal`（改写会话目标）、`self`（内省主循环状态）、`cron`/`mcp` 等
   主 agent 专属能力——这些都不属于"完成单个后台任务"的职责范围。

nanobot 参考实现（`nanobot/agent/subagent.py:197-217 _build_tools`）通过
**scope 过滤**在结构上消除该问题：SubagentManager 自建独立注册表，只装载
声明了 `"subagent"` scope 的工具；spawn/message/goal/memory/self 等核心工具
因 scope 不含 "subagent" 而**天然不可见**。

## 本 step 解决什么问题、为什么这样做

- **解决**：子代理与主 agent 的工具权限边界缺失。
- **为什么选 scope 过滤而非运行时拦截**：
  - nanobot 同款方案，对齐有据；
  - 基础设施已备好：`ToolLoader.load(ctx, registry, scope=...)`（loader.py:55-60）
    早已支持按 `_scopes` 过滤，且 11 个工具已声明含 `"subagent"` 的 scope
    （shell、filesystem×4、search×2、web×2、write_stdin、apply_patch）——
    本 step 只是"接通最后一段线"；
  - 工具不可见 = LLM schema 列表中根本不出现，比执行时报错更省 token、更可靠。

## 方案利弊

| 方案 | 利 | 弊 |
|------|----|----|
| **A. 自建裁剪版 registry（本 step，对齐 nanobot）** | 结构性防递归；对齐源码；改动集中 | 主/子代理工具实例不再共享（file_state/exec_session 生命周期需显式决策） |
| B. 运行时拦截（runner 检查调用方身份） | 实现直观 | 需要贯穿 runner 的身份上下文，复杂度高；schema 仍暴露给 LLM 浪费 token |
| C. 黑名单参数（排除若干工具名） | 最快 | 白名单语义更安全，黑名单易漏新工具 |

选 A。file_state / exec_session 生命周期对齐 nanobot：每次 spawn 新建独立
`FileStateStore`（子代理间互不污染），`ExecSessionManager` 由 manager 持有并跨
子代理共享（长命令会话可被后续管理）。

## 目标

1. `SubagentManager._build_tools()` 构建独立注册表，恰含 11 个 subagent-scope 工具；
2. 子代理运行期使用该注册表，无法感知/调用 spawn 等核心专属工具；
3. 主 agent 工具集与 spawn 行为不受影响；
4. 全部测试 mock 网络，无真实 API 依赖。

## 非目标（留后续 step）

- 子代理内 RequestContext/session_key/workspace_scope 绑定（会话类工具仍不可用）；
- spawn `temperature` 参数与 runtime 冻结派生；
- LLM wall-clock 超时链路；
- system prompt 模板化与 skills/workspace 注入；
- spawn 并发超限拒绝文案补齐（"Wait for a running subagent..." 后缀）。

## 验收标准

- tests/test_subagent_tool_isolation.py 六个用例全绿；
- 既有测试（TestSubagentManager / TestMidTurnInjection / test_max_iterations 等）无回归；
- `python -m step111.main` 可正常装配启动（手动冒烟可选）。
