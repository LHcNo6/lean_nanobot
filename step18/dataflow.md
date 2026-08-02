## Step 18 — Data Flow (ToolLoader & Tool System Upgrade)

---

## 图 0：全局视图（两条链）

```
                    ┌─────────────────────────────────────────────┐
                    │            回合编排链 (AgentLoop)           │
  用户消息           │  RESTORE→COMPACT→BUILD→RUN→SAVE→RESPOND→DONE│
──────────► InboundMessage ──► _dispatch ──► _process_message ──► OutboundMessage ──► 用户
                    └─────────────────────────────────────────────┘
                                        │ RUN 状态
                                        ▼
                    ┌─────────────────────────────────────────────┐
                    │            迭代执行链 (AgentRunner)          │
                    │  RequestContext 绑定 → 每轮:                │
                    │  治理→调LLM→工具调用→结果回填 → 结束复位      │
                    └─────────────────────────────────────────────┘
                                        │
                    ┌───────────────────┼───────────────────────┐
                    ▼                   ▼                        ▼
             ToolLoader/ToolContext  ToolRegistry.prepare_call   ContextVar
             (装配: 工具实例化)        (校验流水线+执行)        (RequestContext)
                    │                                        │
                    ▼                                        ▼
              tools/echo, spawn, long_task            工具内 current_request_context()
```

---

## 图 1：装配阶段（启动/每轮 RUN 前）

```
main.py / AgentLoop.__init__
  │
  ├─ 创建 registry = ToolRegistry()           (tool.py:282)
  ├─ 创建 provider / session_manager / memory / context_builder
  ├─ 创建 subagent_manager / bus
  │
  ▼
_state_run (loop.py:216) ──每轮消息重复执行──►  ToolContext(
  │                                                  config=None, workspace="",
  │                                                  bus, subagent_manager, sessions )
  │                                          │
  │                                          ▼
  │                              ToolLoader().load(tool_ctx, registry, scope="core")
  │                                    │
  │                                    ├─ discover(): pkgutil 扫描 step18.tools
  │                                    │    └─ EchoTool / SpawnTool / CreateGoalTool / UpdateGoalTool
  │                                    ├─ scope 过滤 (_scopes={"core"})
  │                                    ├─ enabled(ctx) 检查
  │                                    ├─ create(ctx): SpawnTool(manager=ctx.subagent_manager)
  │                                    │                CreateGoalTool(sessions=ctx.sessions)
  │                                    ├─ registry.has(name) 幂等
  │                                    └─ registry.register(tool)
  ▼
AgentRunSpec(tools=registry, session_key=session_key, ...) ──► AgentRunner.run(spec)
```

---

## 图 2：回合状态机（`_process_message`，loop.py:168-188）

```
InboundMessage(content, chat_id, session_key)
   │  session_key = session_key_override or session_key or chat_id  (_dispatch:148)
   │  会话锁: lock.locked()? → 放入 pending queue 等待 (loop.py:149-157)
   ▼
┌─────────┐   ok   ┌─────────┐   ok   ┌─────────┐   ok   ┌─────────┐
│ RESTORE │───────►│ COMPACT │───────►│ BUILD   │───────►│  RUN    │
└─────────┘        └─────────┘        └─────────┘        └────┬────┘
  sessions.get_or_create      consolidator.                   │
  (磁盘 JSONL → Session)      maybe_consolidate_by_tokens     │  AgentRunner.run(spec)
                              summary = metadata["_last_summary"]   │ (图 3)
                                                                    │
┌─────────┐   ok   ┌─────────┐   ok   ┌─────────┐   ok   ┌─────────┘
│  DONE   │◄───────│ RESPOND │◄───────│  SAVE   │◄───────┘ result
└─────────┘        └─────────┘        └─────────┘
  OutboundMessage      result.final_content +         import_messages(新消息)  →  sessions.save()
  ←─ bus 返回给用户    stop_reason/tokens 元数据       ─ 后台 maybe_consolidate

BUILD (loop.py:202-214):
  history = session.get_history(max_messages=50, max_tokens=replay_budget)  ← 内存+磁盘
  goal_lines = goal_state_runtime_lines(session.metadata)
  identity += goal_lines
  initial_messages = ContextBuilder.build_messages(history, summary, identity)
     = [system + ...history + user(当前消息)]
```

---

## 图 3：Runner 迭代循环（核心，runner.py:82-451）

```
AgentRunner.run(spec)
  │
  ├─ RequestContext(session_key=spec.session_key)            ← context.py
  ├─ token = bind_request_context(req_ctx)                   ← ContextVar 绑定
  ├─ hook.before_run
  ▼
  _run_loop ── for iteration in range(max_iterations):
     │
     ├─① messages = _GOVERNOR.prepare_for_model(messages)    ← governance.py:50-64
     │     (去占位/去畸形调用/去孤儿结果/回填缺失/预算裁剪/压缩/截断)
     ├─② tools_defs = spec.tools.get_definitions()  ★缓存+排序 (tool.py:336)
     ├─③ response = provider.chat_stream_with_retry(messages, tools_defs)  ← LLMResponse
     │     (流式: hook.on_stream 逐字 → StreamPublishingHook → bus → StreamDeltaEvent)
     │
     ├─④ 分支判断 (response.finish_reason):
     │
     │  ┌── "error" ──────► _error_result, stop_reason="error" ──► 返回
     │  │
     │  ├── "tool_calls" ──► ★工具调用链 (图 4)
     │  │     └─ 执行完 → messages 追加 tool 结果 → continue (下一轮)
     │  │
     │  ├── 内容为空 ──► 重试 ≤2 次 → 最终询问消息 → 再请求一次
     │  │
     │  ├── "length" ──► 追加 assistant + "请继续" → 重试 ≤3 次
     │  │
     │  └── 正常内容 ──► 追加 assistant 消息
     │
     ├─⑤ 注入回调 (可选): injected 消息合并进 messages (≤5 cycles)
     ├─⑥ goal_active_predicate()? ──► 追加"继续目标"消息 → continue (≤12 rounds)
     │
     └─⑦ 返回 AgentRunResult(final_content, messages, tools_used, usage, stop_reason)
  │
  ├─ finally: reset_request_context(token)                   ← ContextVar 复位
  └─ hook.on_finally → 回 loop: _state_save
```

---

## 图 4：工具调用链（runner.py:231-251 + tool.py:391-446）

```
response.tool_calls: [ToolCallRequest(id, name, arguments)]
   │
   ├─ _drop_malformed_tool_calls (空名过滤)
   ├─ _build_assistant_message → messages.append(assistant+tool_calls)
   ├─ _partition_tool_batches (concurrency_safe 分组, ≤10 并发)
   ▼
   _run_tool(name, tc.arguments)
     │
     ├─ tools_used.append(name)
     ├─ spec.tools.prepare_call(name, arguments)        ★tool.py:391 六步流水线
     │    │
     │    ├─ 1. registry._tools.get(name)
     │    │      └─ 未找到 → _suggest_name 模糊建议 → ToolResult.error("not found...")
     │    ├─ 2. isinstance(tool, ContextAware) → tool.set_context(current_request_context())
     │    ├─ 3. _coerce_params: 字符串JSON解析 + {"arguments":...} 展开
     │    ├─ 4. 非 dict → error("parameters must be a JSON object")
     │    ├─ 5. tool.cast_params: str→int/bool/float 深度强转
     │    └─ 6. tool.validate_params: JSON Schema 校验 (Schema.validate_json_schema_value)
     │          └─ 有错 → ToolResult.error("Invalid parameters...")
     │
     ├─ error? ──► return str(error)（直接成为 tool 消息给模型看）
     │
     ├─ result = await tool.execute(**params)           ★工具执行
     │    │
     │    ├─ echo:     纯函数，无需上下文
     │    ├─ spawn:    读 self._manager (来自 create(ctx))
     │    └─ create/update_goal:
     │         req = current_request_context()          ★ContextVar 读取 (long_task.py:51-53)
     │         session_key = req.session_key
     │         sess = self._sessions.get_or_create(session_key)
     │         goal_state 读写 → metadata[GOAL_STATE_KEY]
     │
     └─ _GOVERNOR.normalize_tool_result (截断 >16000 字符)   ← governance.py:76-87
   ▼
   messages.append({"role":"tool", "tool_call_id", "name", "content": str(result)})
   iter_ctx.tool_results.append(...)
   ▼
   continue → 图 3 下一轮迭代（LLM 看到工具结果后继续/收尾）
```

---

## 图 5：持久化链（SAVE 状态）

```
_run_loop 返回 AgentRunResult.messages（含本轮所有 assistant/tool/user 消息）
   │
_state_save (loop.py:255-266):
   │  skip = 1 + len(ctx.history)     # 跳过 system + 复用的旧历史
   │  session.import_messages(result.messages[skip:])   # 追加进内存 Session
   │  sessions.save(session)          # 原子写 JSONL: tmp → os.replace
   │       └─ 每行一条消息; 首行 _type="metadata"(含 goal_state, _last_summary...)
   │  _schedule_background(maybe_consolidate_by_tokens)  # 后台: 超预算 → 摘要替换
   ▼
RESTORE 状态: get_or_create → _load(JSONL) → Session 恢复（下次回合）
```

---

## 图 6：关键数据结构流（各环节的"数据形状"）

```
InboundMessage(content:str, chat_id, session_key)
   │
   ▼ TurnContext(msg, session_key, session, history, initial_messages, result, outbound)
   │
   ▼ AgentRunSpec(initial_messages[], tools=ToolRegistry, provider, max_iterations=5,
   │              hook, session_key, goal_active_predicate, goal_continuation_rounds)
   │
   ▼ messages: list[dict]  ← 全程唯一的"对话状态载体"
   │     [{"role":"system",...}, {"role":"user",...},
   │      {"role":"assistant","tool_calls":[{"id","type","function":{"name","arguments":json}}]},
   │      {"role":"tool","tool_call_id","name","content"}]
   │
   ▼ LLMResponse(content, tool_calls:[ToolCallRequest], finish_reason, usage)
   │
   ▼ (prepare_call) (tool, params, error) → ToolResult(str, is_error)
   │
   ▼ AgentRunResult(final_content, messages, tools_used[], usage, stop_reason)
   │
   ▼ OutboundMessage(content, metadata={stop_reason, tokens}) → bus → 用户
   │
   ▼ Session(JSONL) ←── 持久化
```

---

## 图 7：阅读路径建议（按数据流走一遍）

```
图 2 (回合状态机)
  │  RESTORE→COMPACT→BUILD：Session/history/messages 的组装
  ▼
图 3 (Runner 迭代循环)
  │  重点：finish_reason 五个分支 + continue 语义
  │  （整个 loop 靠"消息追加 + continue"驱动，无显式状态机）
  ▼
图 4 (工具调用链)
  │  prepare_call 六步：同步校验 + 异步执行 的分离
  │  ContextVar 如何让 create_goal 拿到 session_key
  ▼
图 5 (持久化链)
  │  消息如何回到磁盘，闭环完成
  ▼
图 6 (数据结构流) —— 任意时刻回看"当前环节的数据形状"

核心一句话：所有智能都发生在 messages 列表上——
治理函数洗它、LLM 读它、工具结果追加它、最后把它写进 Session；
loop/runner 只是这条数据河的搬运工，
step18 新增的工具系统 (loader/context/schema) 负责让"工具"环节
从手工管理变为声明式 (tool_parameters → create(ctx) → ToolLoader → prepare_call)。
```
