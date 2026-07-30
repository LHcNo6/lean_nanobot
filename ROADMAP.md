# lean_nanobot — 路线图

按最小增量对齐 nanobot 架构，复杂功能拆分为独立步骤。

---

## 已完成

| Step | 主题 | 核心文件 | 测试数 |
|------|------|----------|--------|
| 0–12 | 基础演进 | — | — |
| 13 | Context Governance | governance.py | 104 |
| 14 | Governance 增强 | governance.py + helpers.py | 104 |
| 15 | Consolidation + Dream + MemoryStore | memory.py + consolidation.py | 128 |
| 16 | Subagents + Sustained Goals | subagent.py + long_task.py + goal_state.py | 156 |

---

## Step 17a — Governance & Tool Execution Safety

**主题：** AgentRunner 可靠性增强（基础安全层）

| 改进 | 行数 |
|------|------|
| ContextGovernor 默认集成 | ~20 |
| 并发工具执行 | ~50 |
| 工具结果归一化 | ~20 |
| 格式错误工具调用处理 | ~20 |
| LLM 超时 | ~15 |
| 测试 | ~50 |

**目标测试数：** ~206

---

## Step 17b — Content Recovery & Continuation Control

**主题：** AgentRunner 完成度保障（响应质量）

| 改进 | 行数 |
|------|------|
| 空内容重试 | ~25 |
| Token 耗尽续行 | ~20 |
| Goal 续行封顶 | ~15 |
| 注入周期控制 + 合并 | ~30 |
| 测试 | ~50 |

**目标测试数：** ~256

---

## Step 18 — ToolLoader & Tool System Upgrade

**主题：** 工具系统升级为 nanobot 风格

| 改进 | 说明 |
|------|------|
| ToolLoader | `pkgutil.iter_modules` + `entry_points` 自动发现 |
| 安全边界 | SSRF 保护、workspace 违规检测 |
| ContextVar 状态 | `RequestContext` / `ToolContext` ContextVar 注入 |
| RuntimeContextProvider | 工具向 system prompt 注入运行时上下文块 |
| 参数校验 | `prepare_call()` → JSON Schema 校验 + 类型强转 |
| `Tool` 基类增强 | `read_only`, `exclusive`, `concurrency_safe`, `cast_params`, `validate_params` |

**导入：** 从 step17b fork，import `step17b.` → `step18.`

---

## Step 19 — Session System Upgrade

**主题：** 会话管理升级为 nanobot 风格

| 改进 | 说明 |
|------|------|
| base64url 文件名编码 | 替代 `:` → `_` 转义 |
| 两级缓存 | `OrderedDict` (128 hot) + `WeakValueDictionary` overflow |
| AutoCompact | TTL 驱动的空闲会话压缩 |
| 文件上限强制 | `enforce_file_cap(2000)` |
| Pending user turn restore | 崩溃恢复 |
| Fork session | `fork_session_before_user_index()` |

**导入：** 从 step18 fork，import `step18.` → `step19.`

---

## Step 20 — Channel Framework

**主题：** 通道框架

| 改进 | 说明 |
|------|------|
| BaseChannel ABC | `start()`, `stop()`, `send()`, `_handle_message()` |
| ChannelManager | 发现、初始化、启动/停止，路由 outbound 消息 |
| Permission system | `is_allowed()`, pairing, `allowFrom` |
| Stream delivery | `send_delta()` streaming 支持 |
| CLI channel | 第一个实现 |

**导入：** 从 step19 fork，import `step19.` → `step20.`

---

## 未来候选

| Step | 主题 | 说明 |
|------|------|------|
| 21 | Providers Registry & Factory | nanobot 风格的 provider 注册/匹配/fallback |
| 22 | Configuration | Pydantic config schema |
| 23 | Gateway & HTTP API | WebSocket gateway + OpenAI-compatible HTTP API |
| 24 | MCP Integration | Model Context Protocol tools |

---

## 设计原则

1. **最小增量** — 每步只改最少的文件，独立可测试
2. **向后兼容** — AgentRunSpec、AgentLoop 接口只加可选字段
3. **可拆分** — 复杂功能跨步骤，步间可通过 fork + import 变更串联
4. **测试先行** — 每步增加相应测试，不破坏原有测试
