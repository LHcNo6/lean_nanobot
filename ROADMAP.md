# 从零构建 nanobot：最小增量路线图

共 **7 个阶段 19 步**，每步都是可独立运行的最简增量。

---

## Phase 0：最简 LLM 调用

### Step 0 — 裸 HTTP 调用（~30 行）
- 用 `httpx` POST 到 OpenAI-compatible API
- 硬编码 URL / API key / model
- `python main.py "你好"` → 打印回复
- 纯同步，单文件，没有任何抽象

---

## Phase 1：Provider 抽象

### Step 1 — Provider 基类 + OpenAI 实现（+2 文件）
- `LLMProvider(ABC)` with `async chat(messages) → LLMResponse`
- `OpenAICompatProvider` 实现
- `LLMResponse(content, tool_calls, finish_reason)` dataclass
- `main.py` 改为 async，效果同上但可复用

### Step 2 — 流式支持
- `chat_stream(messages, *, on_content_delta)` 默认回退到 `chat`
- `OpenAICompatProvider` 覆盖为真正的 SSE 流式
- CLI 逐 token 打印输出

### Step 3 — 带重试的调用
- `chat_with_retry()` / `chat_stream_with_retry()` 封装重试逻辑
- 区分临时错误 (429/5xx) vs 不可重试 (quota)

---

## Phase 2：Agent Runner + 工具

### Step 4 — Tool 基类 + Registry
- `Tool(ABC)` with `name`, `description`, `parameters` (JSON Schema)
- `ToolResult(content, is_error)`
- `ToolRegistry.register()`, `get_definitions()` (输出 OpenAI 格式)
- 一个工具：`echo_tool`（回显参数，测试用）

### Step 5 — AgentRunner 单轮工具
- `AgentRunner.run(spec: AgentRunSpec)`：
  1. 发送 messages + tools → LLM
  2. 如果有 `tool_calls` → `ToolRegistry.execute()` → 追加 result
  3. 继续发回 LLM（最多 `max_iterations` 次）
  4. 直到 LLM 返回文本 → 结束

### Step 6 — ContextBuilder（系统提示）
- `ContextBuilder.build_system_prompt()` 组装 identity
- 读取 `AGENTS.md` / `SOUL.md` / `USER.md` 作为引导文件
- `build_messages(history, current_message)` → `[system, *history, user]`

---

## Phase 3：多轮会话

### Step 7 — Session
- `Session(key, messages[], metadata{})` dataclass
- `SessionManager.get_or_create(key)` → 从文件加载 / 新建
- JSON 文件持久化 `workspace/sessions/{key}.json`
- `add_message(role, content)` / `get_history(max_messages)`

### Step 8 — 自动压缩
- `last_consolidated` 指针标记已处理边界
- 超 `max_messages` / `max_tokens` 时截断老消息
- `Consolidator.archive(messages)` → LLM 摘要 → `history.jsonl`

---

## Phase 4：AgentLoop 状态机 + 总线

### Step 9 — MessageBus
- `MessageBus()`：两个 `asyncio.Queue`（inbound / outbound）
- `InboundMessage(channel, sender_id, chat_id, content)`
- `OutboundMessage(channel, chat_id, content)`

### Step 10 — AgentLoop 状态机（最简版 5 态）
```
RESTORE → BUILD → RUN → SAVE → RESPOND → DONE
```
- `_process_message()` → while state != DONE: `handler = getattr(self, f"_state_{state}")`
- `_state_build()` → ContextBuilder 组装 messages
- `_state_run()` → AgentRunner.run()
- `_state_save()` → Session.add() + 持久化
- `run()` 主循环：`while running: msg = bus.consume_inbound() → _dispatch(msg)`

---

## Phase 5：流式 + 钩子

### Step 11 — Hook 系统
- `AgentHook` with `before_run`, `after_run`, `before_iteration`, `after_iteration`, `on_stream`, `on_error`
- `AgentRunHookContext` / `AgentHookContext` 传递状态
- `AgentRunner.run()` 注入 hook 生命周期

### Step 12 — 流式集成到 AgentLoop
- `on_stream` / `on_stream_end` callbacks → bus 发布 `StreamDeltaEvent`
- 流式时阻断最终响应直到流结束

---

## Phase 6：高级特性

### Step 13 — 回合中注入 (Mid-turn injection)
- `_pending_queues[session_key] = asyncio.Queue(maxsize=20)`
- `AgentRunner` 通过 `injection_callback` 抽取注入消息
- 每轮工具执行后 / 最终响应前 drain

### Step 14 — Context Governance
- `ContextGovernor.prepare_for_model()`: 角色轮换修复、孤儿 tool_result 清理、空内容填充
- `estimate_message_tokens()` 预算检查
- `ContextGovernanceConfig` 配置

### Step 15 — Consolidation + Dream
- Token 预算 consolidation：BUILD 阶段检查，超标则摘要归档
- Dream 阶段：临时 agent 回合把 `history.jsonl` 蒸馏到 `MEMORY.md`
- `.dream_cursor` 追踪处理位置

### Step 16 — Subagents + Sustained Goals
- `SubagentManager`: 生成子 agent 回合，结果作为注入消息返回
- 目标状态跟踪：`goal_state_runtime_lines()` → 自动注入 "继续工作" 消息
- `long_task.py`: 长时间运行任务支持

---

## Phase 7：通道 + 插件体系

### Step 17 — Channel 抽象
- `Channel(ABC)` with `start()`, `stop()`, 内部 consume_outbound
- CLI channel、WebSocket channel 实现
- 通过 MessageBus 与 AgentLoop 解耦

### Step 18 — MCP + Skills + 插件
- MCP 服务器连接 → 工具自动发现
- `SkillsLoader.load_skills_for_context()` → 注入系统提示
- `ToolLoader` pkgutil 自动发现 + entry_points 插件

### Step 19 — Config 体系
- Pydantic schema + JSON 文件加载
- Provider/工具/通道配置

---

## 关键设计原则

```
每步完成后都能 RUN 起来看到效果
每个新功能只增加一个文件或给已有文件加一个方法
绝不提前抽象不需要的东西
```
