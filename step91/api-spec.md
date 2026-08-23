# Step 91 API Specification

## 1. ToolContext 增强

**文件**：`context.py`

新增字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `agent_loop` | Any | AgentLoop 引用（可选，用于 MyTool 自省） |

## 2. MyTool 嵌套属性访问

**文件**：`tools/self.py`

### key 格式

支持点分路径：
- 单层：`workspace`, `config`, `exec_timeout`
- 嵌套：`config.exec.timeout`, `agent.iteration`, `config.tools.restrict_to_workspace`

### 顶级 key

| key | 映射到 |
|-----|--------|
| `workspace` | ctx.workspace |
| `session_key` | ctx.session_key |
| `config` | ctx.config |
| `agent` | ctx.agent_loop |
| `exec_config` | ctx.config.exec |
| `web_config` | ctx.config.web |
| `exec_timeout` | ctx.config.exec.timeout |
| `web_timeout` | ctx.config.web.timeout |
| `tool_count` | 估算工具数量 |
| `iteration` | ctx.iteration |

### 安全边界

嵌套路径的每一段都检查：
- `_DENIED_ATTRS`：Python 内部属性（__class__, __dict__ 等）
- `_BLOCKED`：核心基础设施（bus, provider, tools 等）

遇到禁止属性时返回 `ToolResult.error`。

### set 操作

仍然只支持单层白名单属性（exec_timeout, web_timeout, max_tool_result_chars），
不支持嵌套 set。
