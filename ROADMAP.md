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
| 17a | Governance & Tool Execution Safety | runner.py + governance.py | 206 |
| 17b | Content Recovery & Continuation Control | runner.py + loop.py | 256 |
| 18 | ToolLoader & Tool System Upgrade | loader.py + tool.py + context.py | — |
| 19 | Session System Upgrade | session.py + autocompact.py | — |
| 20 | Channel Framework | channel.py + pairing.py + manager.py + channels/ | 318 |

---

## Step 21 — CommandRouter & COMMAND 状态

**主题：** 对齐 nanobot loop 8 态状态机（`RESTORE→COMPACT→COMMAND→BUILD→RUN→SAVE→RESPOND→DONE`）

| 改进 | 说明 |
|------|------|
| `command/router.py` | `CommandRouter` 三档路由（priority/exact/prefix）+ `normalize_command_text` + `CommandContext` |
| `command/builtin.py` | `/dream` `/history` `/new` `/pairing`（接 PairingStore）`/help` |
| `loop.py` | 加 `COMMAND` 态，命令短路 `shortcut→DONE` |
| `main.py` | 删除 on_command 闭包（`/exit` 保留 CliChannel） |

**导入：** 从 step20 fork，import `step20.` → `step21.`

---

## Step 22 — Providers Registry & Factory + Fallback

**主题：** provider 注册/匹配/异常式回退（nanobot providers/registry + factory + fallback_provider 最小集）

| 改进 | 说明 |
|------|------|
| `providers/registry.py` | `ProviderSpec` dataclass + ~6 条目（openai/deepseek/dashscope/openrouter/ollama/custom）+ `find_by_name` |
| `providers/factory.py` | `make_provider(settings)`，模型名关键词匹配 |
| `providers/fallback_provider.py` | 异常捕获式逐级回退（复用 `_StreamGuard` 已发 delta 不重试） |

**导入：** 从 step21 fork，import `step21.` → `step22.`

---

## Step 23 — Pydantic 配置系统

**主题：** nanobot config/schema + loader 最小集（`NANOBOT_` env 前缀 + JSON 文件）

| 改进 | 说明 |
|------|------|
| `config/schema.py` | Config（agents.defaults / providers / channels / model_presets） |
| `config/loader.py` | 配置文件加载 + env 解析 |
| 接入 | 消除 main.py 硬编码常量；工厂改接 Config；`Tool.config_cls()` 落地 |

**导入：** 从 step22 fork，import `step22.` → `step23.`

---

## Step 24 — Gateway & HTTP API

**主题：** OpenAI 兼容 HTTP API（nanobot api/server.py 最小集）

| 改进 | 说明 |
|------|------|
| `api/server.py` | `POST /v1/chat/completions`（SSE 流式）+ `GET /v1/models` + `GET /health` |
| 鉴权 | Authorization 中间件；请求入 bus → loop → outbound 转 SSE |

**导入：** 从 step23 fork，import `step23.` → `step24.`

---

## Step 25 — MCP Integration

**主题：** Model Context Protocol 工具集成（nanobot agent/tools/mcp.py 最小集）

| 改进 | 说明 |
|------|------|
| `agent/tools/mcp.py` | stdio / streamable HTTP 客户端，`mcp_<server>_<tool>` 注册 |
| 配置 | `tools.mcp_servers`（接 step23 config）；测试全部 mock |

**导入：** 从 step24 fork，import `step24.` → `step25.`

---

## 未来候选

| Step | 主题 | 说明 |
|------|------|------|
| 26+ | WebUI / Skills / Triggers / entry_points 插件发现 | 视需求规划 |

---

## 设计原则

1. **最小增量** — 每步只改最少的文件，独立可测试
2. **向后兼容** — AgentRunSpec、AgentLoop 接口只加可选字段
3. **可拆分** — 复杂功能跨步骤，步间可通过 fork + import 变更串联
4. **测试先行** — 每步增加相应测试，不破坏原有测试
