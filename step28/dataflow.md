# Step 28 — Data Flow (Runtime Context + Workspace Binding)

step28 新增/变更的链条：① A9 运行时上下文链（provider 注册 → BUILD 态解析 → 追加进内存 user 消息，历史零污染）② 富 RequestContext 构造链（普通 turn 与 system 通道复用同一构造器）③ workspace 生产端装配链（config → from_config → WorkspaceScopeResolver → ToolContext/AgentRunSpec 真值注入）④ runner 双 ContextVar 绑定链（request_context + workspace_scope，finally 复原）⑤ 工具执法消费链（read_file → current_tool_workspace → resolve_allowed_path 边界强制）。回合状态机 8 态、出站链、provider 链、装配链（step21/22/25 成果）零改动；step27 skills 链路零改动（仅路径前缀改名）。

---

## 图 0：全局视图（双 ContextVar + 五条新链）

```
   ┌──────────────────── 回合编排链 (step21 原样，8 态) ─────────────────────┐
   │ RESTORE→COMPACT→COMMAND→BUILD→RUN→SAVE→RESPOND→DONE                     │
   │                                                                         │
 ①A9运行时上下文链             ③workspace生产端装配链               ⑤工具执法链
 main.py:94 clock provider    config.tools.restrict_to_workspace   read_file.execute(72)
   │ register_runtime_          (schema.py:175)                     │ current_tool_workspace(76)
   │ context_provider(313)        │ from_config(283)                │   ├─ ContextVar 有绑定→scope
   ▼                            ▼                                 │   └─ 无绑定→构造参数回退
 _state_build(549)             AgentLoop.restrict_to_workspace     ▼
   │ _build_turn_               │  + workspace_scopes=             resolve_allowed_path(81)
   │ request_context(557)      │ WorkspaceScopeResolver(189)       │ workspace_policy.py:99
   │ _resolve_runtime_          ▼                                   │  受限→allowed_root+skills豁免
   │ context_for_turn(561)     _build_agent_spec(576)              │  越界→WorkspaceBoundaryError
   ▼                            │ for_message(scope 解析)           ▼
 build_messages(571)           ▼                                 ToolResult.error(+注记)
  │ append_runtime_context     ToolContext(workspace=scope.        │
  │   (context.py:217)         │  project_path, restrict=scope.    │
  ▼                            │  restrict_to_workspace)           │
 initial_messages(仅内存)      spec.request_context +             │
  ▲                            spec.workspace_scope               │
  └── ②富RequestContext ◄──────┘        │                          │
      (loop.py:322 双路径复用)          ▼                          │
                                ④runner 绑定链(runner.py:117-123) │
                                  bind_request_context ◄──────────┘
                                  + bind_workspace_scope
                                  工具执行中 current_tool_workspace()
                                  finally: reset×2 (147-149) 无泄漏
```

---

## 图 1：A9 运行时上下文链（★全新，metadata 文本不进历史）

```
main.py:94  _clock_runtime_context: async (request) -> RuntimeContextBlock | None
   │   （request.session_key 为空 → None；否则 wrap 当前 UTC 时间）
   ▼
loop.register_runtime_context_provider(provider)      (loop.py:313，重复注册去重)
   │   loop._runtime_context_providers: list[Any]     (loop.py:194)
   ▼
_state_build (loop.py:549)  ──┐                 _process_system_message (loop.py:724)
  ctx.request_context =       │                 request_context = _build_turn_request_context(763)
    _build_turn_request_      │                   runtime_context_blocks = resolve_runtime_context(
    context(557)              │                     [*registry.get_runtime_context_providers(),   (766)
  blocks = _resolve_runtime_  │                      *_runtime_context_providers], request)        (768)
    context_for_turn(561)     │                   scope = workspace_scopes.for_message(...)        (770)
  scope = for_message(564)    │                   initial_messages = build_messages(...,            (774-778)
      │                       │                     runtime_context_blocks=..., workspace=...)
      ▼                       ▼
  provider 双源合并（顺序固定）:
    ├─ 工具自带 provider: ToolRegistry.get_runtime_context_providers()
    │    （tool.py:322；按工具名排序；Tool.runtime_context_provider() 基类返回 None）
    └─ loop 级 provider（注册顺序）
      ▼
  resolve_runtime_context(providers, request)        (runtime_context.py:83)
    │ 串行 await 每个 provider（★稳定顺序，无并发）
    │ normalize_runtime_context_blocks(65): 剥首尾空白 / 空内容丢弃 / source 非空校验
    ▼
  build_messages(current_message, history, …, runtime_context_blocks)   (context.py:190)
    │ blocks = blocks 仅当 current_role == "user"                        (216)
    │ merged, _meta = append_runtime_context(current_message, blocks)    (217)
    │   文本形态: f"{text}\n\n{suffix}" / 空文本不加分隔                  (runtime_context.py:94)
    │   多模态形态: 追加 {"type":"text",…} 块                             (109-115)
    │   marker（sources/suffix）已返回但 lean 不持久化 ★取舍
    ▼
  messages = [system, *history, tail(user + 上下文块)]  → runner
    ▲
    └── 历史零污染: ctx.session.add_message("user", ctx.msg.content)(567)
         写在解析之前 —— session.messages 永远保留原始文本
★测试锚点: test_state_build_attaches_blocks_in_memory_only
          (test_runtime_context.py:237)  断言 initial 含块 / session 历史不含
★对照 nanobot: runtime_context.py 同一模型；差异=lean 不持久化 marker
```

---

## 图 2：富 RequestContext 构造链（★升级，双路径复用）

```
普通 turn: _state_build (549)                      system 通道: _process_system_message (763)
   └──────────────────────┬──────────────────────────┘
                          ▼
   _build_turn_request_context(msg, session, session_key,     (loop.py:322)
                                  *, runtime=None, turn_id=None)
      │ scope = workspace_scopes.for_message(msg, session.metadata)
      ▼
   RequestContext(                                          (context.py:21)
      channel=msg.channel,
      chat_id=msg.chat_id,
      message_id=metadata.get("message_id"),
      session_key=session_key,
      original_user_text=msg.content,
      runtime=runtime,                 ★新增 (step27 无)
      metadata=dict(msg.metadata),
      sender_id=msg.sender_id,         ★新增
      turn_id=turn_id,                 ★新增
      workspace=scope.project_path,    ★新增 —— scope 解析结果直达工具
   )
      │
      ├─► 存进 TurnContext.request_context (loop.py:80/557)
      ├─► 传给 _resolve_runtime_context_for_turn (361-362)   ← A9 的 request
      └─► 传给 _build_agent_spec(..., request_context=...)    ← runner 绑定（图 4）
★意义: step27 里 runner 只回退到 RequestContext(session_key=…) 的最小形态；
      step28 两条消息路径统一拿到完整 turn 快照，工具/上下文 provider 均可查。
```

---

## 图 3：workspace 生产端装配链（★全新，config → 工具装配）

```
config.tools.restrict_to_workspace: bool = False          (config/schema.py:175)
   │   ★默认与 nanobot 一致：不默认锁死用户路径
   ▼
AgentLoop.from_config(…)                                   (loop.py:283)
   │ restrict_to_workspace = extra.pop("restrict_to_workspace",
   │                      getattr(config.tools, "restrict_to_workspace", False))
   ▼
AgentLoop.__init__(restrict_to_workspace=False, …)         (loop.py:186)
   │ self.restrict_to_workspace = restrict_to_workspace
   │ self.workspace_scopes = WorkspaceScopeResolver(
   │     default_workspace=context_builder.workspace,      (189-192)
   │     default_restrict_to_workspace=restrict_to_workspace)
   ▼
_build_agent_spec(msg, session_key, session, initial,      (loop.py:576)
                 *, request_context=None, workspace_scope=None)
   │ scope = workspace_scope or self.workspace_scopes.for_message(   (587-590)
   │            msg, session.metadata if session else None)
   │ tool_ctx = ToolContext(                                 (592-596)
   │     config=self.config,
   │     workspace=str(scope.project_path),      ★step27: 硬编码 workspace=""
   │     restrict_to_workspace=scope.restrict_to_workspace,   ★新增字段(context.py:82)
   │     bus/bus, subagent_manager/sessions/session_key …)
   ▼
   ToolLoader().load(tool_ctx, self.registry, scope="core")  → 工具真值装配
      └─ read_file.create(ctx) 取 ctx.workspace / ctx.restrict_to_workspace
         (read_file.py:44-53)  → loop.registry 可断言 (tests/test_workspace_tool.py)
   │
   ▼
   AgentRunSpec(..., request_context=request_context,        (loop.py:620-621)
                workspace_scope=scope)
★测试锚点: TestLoopToolAssembly.test_read_file_registered_with_real_workspace
           TestLoopRuntimeContext.test_workspace_restriction_wiring
```

---

## 图 4：runner 双 ContextVar 绑定链（★升级，生命周期=单次 run）

```
AgentRunner.run(spec)                                        (runner.py:107-149)
   │
   │ req_ctx = spec.request_context                           (117)
   │        or RequestContext(session_key=spec.session_key)   ★回退保持旧行为兼容
   │ token = bind_request_context(req_ctx)                    (118, context.py:55)
   │ ws_token = bind_workspace_scope(spec.workspace_scope)    (121-123)
   │            if spec.workspace_scope is not None            ← None 时不绑定
   ▼
   … run 迭代（chat / 工具执行 / 注入 …）
      │
      ▼  工具执行中（ContextVar 同步可见，含子任务）
   current_request_context()      → RequestContext（channel/turn_id/workspace…）
   current_workspace_scope()      → WorkspaceScope（project_path/restrict…）
   current_tool_workspace(ws, restrict_to_workspace=…)        (workspace_access.py:410)
      ├─ scope 绑定存在 → 以 scope 为准（project_path / restrict_to_workspace）
      └─ 无绑定 → 回退构造参数 + sandbox_restricts_workspace 追加限制
   ▼
   finally:                                                  (147-149)
      if ws_token is not None: reset_workspace_scope(ws_token)   (400)
      reset_request_context(token)                              (context.py:59)
★测试锚点: TestRunnerBinding.test_scope_restored_after_run（run 后双 ContextVar 均为 None）
           test_fallback_minimal_context_without_spec_fields（无 spec 字段→最小回退）
★决策: ContextVar 在 async 迭代间共享；如需并发工具显式隔离再按需处理（step28.md 取舍记录）
```

---

## 图 5：工具执法消费链（★全新，read_file 边界强制）

```
ReadFileTool.create(ctx) → ReadFileTool(workspace=ctx.workspace,
                                        restrict_to_workspace=ctx.restrict_to_workspace)  (read_file.py:44)
   │
   ▼
read_file.execute(path="", max_chars=60_000)                  (read_file.py:72)
   │ path 为空 → ToolResult.error("requires a 'path' parameter")   (74)
   │ access = current_tool_workspace(self._workspace,          (76-79)
   │             restrict_to_workspace=self._restrict)
   ▼
resolve_allowed_path(path,                                    (read_file.py:81)
     workspace=access.project_path or self._workspace,
     allowed_root=access.allowed_root,          ← 受限时 = 项目根 (workspace_access.py:136)
     extra_allowed_roots=[BUILTIN_SKILLS_DIR]   ← 受限时豁免内置技能目录(87) ★对齐 nanobot extra_read
     )                                                          (workspace_policy.py:99)
   │ 解析语义:
   │  相对路径 → 按 workspace 解释 (resolve_path, 26-31)
   │  绝对路径 → 直通
   │  受限 (allowed_root 存在): 必须落于 allowed_root ∪ extra roots 之一
   │    否则 WorkspaceBoundaryError(131) + WORKSPACE_BOUNDARY_NOTE(15)
   │      "hard policy boundary, do not retry with shell tricks…"
   │  未受限: 无边界，仅按 workspace 解析相对路径
   ├─ WorkspaceBoundaryError → ToolResult.error(f"Error: {exc}")   (89-90)
   └─ 通过 →
   read_text(utf-8, errors="replace")
      │ FileNotFoundError → "File not found" | IsADirectoryError → "Is a directory"
      ▼
   len(text) > max_chars → text[:max_chars] + "\n...[truncated]"   (103-104)
      ▼
   ToolResult("```\n{text}\n```")
★测试锚点: TestReadFileBoundary 9 用例（workspace 内/相对路径/越界/../逃逸/
           full 模式/技能豁免/截断/缺失/缺参）(test_workspace_tool.py)
★对照 nanobot: filesystem 工具 extra_read_allowed_dirs 豁免语义
```

---

## 图 6：WorkspaceScope 解析路由决策树（★全新，workspace_access.py:144）

```
WorkspaceScopeResolver(default_workspace, default_restrict_to_workspace, scoped_channel="websocket")
   │
   ├─ default() (163) ──────────────► default_workspace_scope(workspace, restrict)
   │                                      └─ build_workspace_scope(266):
   │                                          路径 expanduser+resolve(strict=False)
   │                                          access_mode: restricted/full (由 restrict 反推, 261)
   │                                          sandbox_status(212): off / system / application
   │                                             (NANOBOT_WORKSPACE_SANDBOX_ENFORCED/_PROVIDER 两 env, 456)
   │
   ├─ for_message(msg, session_metadata) (170) ──► for_turn(channel=msg.channel, …)
   │
   └─ for_turn(channel, message_metadata, session_metadata) (182)
        │
        ├─ channel != "websocket" (scoped_channel) ──► default()      ★CLI 一律默认
        │
        └─ channel == "websocket" ──► resolve_effective_workspace_scope(371)
             ├─ message_metadata 含 workspace_scope 键 → workspace_scope_from_metadata(342)
             │     └─ validate_workspace_scope_payload(302):
             │          project_path 必填/绝对/目录存在，否则 WorkspaceScopeError(400)
             │          损坏/非法数据 → 安全回退 default()
             └─ 否则 → session_metadata 兜底（同上）
   ▼
WorkspaceScope(project_path, access_mode, restrict_to_workspace,   (82)
               sandbox_status, source_channel)
   │ 工具侧视图: ToolWorkspace(project_path, restrict_to_workspace, scope) (122)
   │   └─ allowed_root 属性 (136): 受限且 project_path 存在 → project_path，否则 None
   ▼
persist_message_scope(session, msg) (200): websocket 消息声明的 scope
   落到 session.metadata["workspace_scope"]（后续轮次复用）
★测试锚点: TestWorkspaceScopeResolver（default/websocket metadata 覆盖/损坏回退）
           TestWorkspaceScopeContextVar（bind/reset 嵌套/恢复）(test_security.py)
★对照 nanobot: workspace_access.py 同一解析路由；差异=无 WebUI 场景，
   scoped_channel 默认 "websocket"（CLI 走 default() 分支）
```

---

## 图 7：阅读路径建议

```
图 0 (全局)        ← 先建立心智模型：8 态状态机不动，五条链挂在 BUILD/装配/RUN 三点
   │
   ▼ A9（动态文本）
图 2 (富 RequestContext) ← 一切请求元数据的构造源（双路径复用）
图 1 (运行时上下文链)    ← provider 双源 → 串行解析 → 仅内存追加，历史零污染
   │
   ▼ A10（路径边界）
图 3 (装配链)        ← config.restrict → WorkspaceScopeResolver → ToolContext/spec 真值
图 4 (绑定链)        ← runner 双 ContextVar：run 前 bind、finally reset，工具可查
图 5 (执法链)        ← read_file 是首个消费者：allowed_root + skills 豁免 + 明确错误
   │
   ▼ 横向
图 6 (scope 解析树)  ← metadata 覆盖/回退/sandbox 三级状态一次看全
核心一句话：step28 把"每 turn 动态上下文"与"workspace 边界"做成可查询的运行时事实——
A9 走消息拼接、只进内存 initial_messages 不进历史；A10 走双 ContextVar，
从 config 一路贯通到工具内部（current_tool_workspace）并在 run 结束时复原。
下一步 step29: 历史可见性（session/history_visibility.py、HIDDEN_HISTORY_META），
A9 的 marker 语义（public_history_message 展示期移除）届时评估对齐。
```

---

## 系统全景图（Step 28 全量）

```
                         ┌────────────────────────────────────────────────────────┐
                         │  配置装配（step25 成果 + step28 贯通）                    │
                         │  load_config → from_config(loop.py:283)                 │
                         │   └ tools.restrict_to_workspace(175) → restrict 意图     │
                         └───────────────────┬────────────────────────────────────┘
                                             │
┌──────────┐   inbound   ┌──────────┐        ▼
│ 通道层     │───────────►│ MessageBus│  ┌──────────── 回合状态机 8 态（step21 原样）────────────┐
│ CliChannel│◄───────────│ (bus)    │  │ RESTORE→COMPACT→COMMAND→BUILD→RUN→SAVE→RESPOND→DONE   │
│  (REPL)   │  outbound  │          │  │ BUILD(549):                                        │
└────┬─────┘            └────┬─────┘  │  ├ 2. request_context = _build_turn_request_context(557)│
     │ send/send_delta       │        │  ├ 3. blocks = _resolve_runtime_context_for_turn(561)  │
     │ (step26 原样)         │        │  ├ 4. scope = for_message(564)                        │
     │                       │        │  └ 5. build_messages(…, runtime_context_blocks,       │
     │                       │        │         workspace)(571)   ◄── A9 追加(仅内存)          │
     │                       │        │ RUN(819):                                           │
     │                       │        │  ├ spec=_build_agent_spec(576): ToolContext 真值 +   │
     │                       │        │  │    spec.request_context/workspace_scope           │
     │                       │        │  ├ runner.run(spec)(图4: 双 ContextVar bind)          │
     │                       │        │  │   ├ provider.chat_with_retry（step22 原样）        │
     │                       │        │  │   ├ 工具执行 → registry.execute → tools/*          │
     │                       │        │  │   │    ├ echo/long_task/spawn（step27 原样）        │
     │                       │        │  │   │    └ read_file ★step28                        │
     │                       │        │  │   │         └ current_tool_workspace(76)          │
     │                       │        │  │   │              → resolve_allowed_path(81)       │
     │                       │        │  │   │                   → 边界/skills豁免/截断       │
     │                       │        │  │   └ finally: reset 双 ContextVar(147-149)         │
     │                       │        │  └ hook.wants_streaming / 流式标记（step26 原样）     │
     │                       │        │ SAVE/RESPOND/finally（step24/26 原样）               │
     │                       │        └──────────────────────────┬───────────────────────────┘
     ▼                       │                                   │
 manager 路由(step26 原样)    │                          RuntimeEventBus（step26 原样）
  ├─ StreamDelta/End→send    │                                   └ 订阅者
  ├─ Progress/RetryWait→门控 │  system 专线: _process_system_message(724)
  └─ 其余→send+退避           │    └ 同样: RequestContext(763) + runtime blocks(766)
                             │        + build_messages(778) + spec 双字段(789-790)  ★双路径一致
```

---

*图中的英文词条为 step28 工程内英文常量/标识，行号以 step28 源码实测为准。*