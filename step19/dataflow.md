## Step 19 — Data Flow (Session System Upgrade)

相对 step18 新增/变更的三条链：
① 回合主链新增「预存/恢复/摘要注入/文件上限」四个钩子
② 新增 AutoCompact 后台压缩链（后台任务，与主链并行）
③ 会话存储层改为 base64url + 两级缓存 + legacy 迁移

---

## 图 0：全局视图（三条链）

```
                    ┌───────────────────────────────────────────────────┐
                    │            回合编排链 (AgentLoop)                  │
  用户消息           │ RESTORE→COMPACT→BUILD→RUN→SAVE→RESPOND→DONE       │
 ────────► InboundMessage ──► _dispatch ──► _process_message ──► Outbound ──► 用户
                    └───────┬──────────────┬──────────────┬─────────────┘
                            │              │              │
                     消息到达时触发          │           SAVE 落盘
                            ▼              ▼              ▼
              ┌───────────────────┐  ┌────────────────────────────┐
              │ ③ 会话存储层        │  │ ② AutoCompact 后台压缩链    │
              │ base64url 文件     │  │ check_expired ─► _archive  │
              │ 两级缓存 hot+weak  │  │ ─► compact_idle_session    │
              │ legacy 自动迁移    │  │ ─► _last_summary ─► 注入摘要│
              └───────────────────┘  └────────────────────────────┘
```

---

## 图 1：会话存储层（session.py:229-403，对应 step18 图 5 的底层）

```
key ("default" / "channel:chat")
   │
   ├─ _storage_key(key) = base64.urlsafe_b64encode(key).rstrip("=")   (session.py:262-264)
   │      "a:b" → "YTpi"   "a_b" → "YV9i"   ★无碰撞（对比 step18 safe_filename 转义）
   ▼
_get_session_path(key) = sessions/{b64key}.jsonl                      (session.py:278-280)

get_or_create(key)  (session.py:304-314)
   │
   ├─ ① _cached(key)                       ★两级缓存查询 (session.py:245-254)
   │      hot(_cache, OrderedDict LRU) 命中 → move_to_end → 返回
   │      miss  → overflow(_overflow_cache, WeakValue) 命中 → _remember 提升 → 返回
   │      miss  → 落盘加载
   ├─ ② _load(key)                         (session.py:316-360)
   │      base64 路径存在 → 逐行读 JSONL:
   │        首行 _type="metadata" → metadata/created_at/updated_at/last_consolidated
   │        其余行 → messages[]（每行一条消息）
   │      base64 路径不存在 → legacy lossy 路径(_get_legacy_lossy_path)存在
   │         → _stored_key_for_path 校验文件内 key 防误迁 → shutil.move 迁移  ★新增
   │      Session 构造 → __post_init__ 钳制越界 last_consolidated  ★新增 (session.py:53-60)
   └─ ③ 无文件 → Session(key=...) 全新
   → _remember(session)                     ★LRU 逐出 (session.py:236-243)
        hot 满 128 → popitem(last=False) → 移入 overflow（WeakValue 保身份）
   → 返回 Session

save(session, fsync=False)  (session.py:362-398)
   → 写 {key}.jsonl.tmp（metadata 行 + 消息逐行, ensure_ascii=False）
   → fsync 可选（文件 + 目录）→ os.replace 原子替换 → _remember
```

---

## 图 2：回合状态机（loop.py:139-330，标注 ★ = step19 新增/变更）

```
InboundMessage
   │ _dispatch: session_key 解析 + 会话锁（locked → pending queue 排队） (loop.py:158-168)
   ▼
   auto_compact.check_expired(★后台触发, loop.py:143-147)  → 见 图 5
   ▼
┌─────────┐ ok ┌─────────┐ ok ┌─────────┐ ok ┌─────────┐
│ RESTORE │───►│ COMPACT │───►│ BUILD   │───►│  RUN    │
└────┬────┘    └────┬────┘    └────┬────┘    └────┬────┘
     │              │              │              │
 ① get_or_create  ③ prepare_session ④ get_history   AgentRunner.run(spec)
 ② ★restore pending  + maybe_consolidate  ★add_message(user)  (图 3)
    有标记且尾是user      + 兜底 _last_summary  ★mark + save
    → 补assistant错误                                              
    → save            skip = 2+len(history)（★step18 为 1+）
┌─────────┐ ok ┌─────────┐ ok ┌─────────┐ ok ┌─────────┘
│  DONE   │◄───│ RESPOND │◄───│  SAVE   │◄───┘ result
└─────────┘    └─────────┘    └────┬────┘
  OutboundMessage      final_content   ⑤ import_messages(skip:)（从 assistant 回复起，与预存 user 衔接）
                       + 元数据        ⑥ ★clear_pending_user_turn
                                       ⑦ ★enforce_file_cap(2000, on_archive=raw_archive)
                                       ⑧ save → ⑨ 后台 maybe_consolidate

各状态详解（★=新增）：
RESTORE (loop.py:201-205):
  ① ctx.session = get_or_create(session_key)        ← 图 1 存储层
  ② _restore_pending_user_turn(ctx.session):        (loop.py:314-330)
     metadata["pending_user_turn"] 存在 且 尾消息 role=="user"
       → 追加 {"role":"assistant","content":"Error: Task interrupted before a response was generated."}
       → 清标记 → save    ★崩溃恢复：把"孤儿 user 消息"合拢成完整 turn

COMPACT (loop.py:207-218):
  ③ ctx.session, pending = auto_compact.prepare_session(session, key)   ★图 5-6
     ctx.summary = pending
     若无 pending → 兜底读 metadata["_last_summary"]["text"]（保持 step18 行为）
     maybe_consolidate_by_tokens: 未合并消息估 token > 预算
       → pick_consolidation_boundary → archive(LLM 摘要/失败 raw_archive)
       → last_consolidated 前移 → _last_summary 写入 → save     (consolidation.py:175-226)

BUILD (loop.py:220-235):
  ④ history = session.get_history(max_messages=50, max_tokens=replay_budget)
        = messages[last_consolidated:]（token 预算倒序贪心 + 尾部 max_messages 截断）
     goal_lines → identity 追加
  ★ 预存当前用户消息（history 之后、build_messages 之前，防 history 重复）:
     session.add_message("user", ctx.msg.content)
     _mark_pending_user_turn(session)          → metadata["pending_user_turn"]=True
     sessions.save(session)                    ← ★崩溃时消息已在磁盘
     initial_messages = build_messages(current_message=content, history, identity,
                                        session_summary=ctx.summary)
        = [system(identity+AGENTS.md+... + [Archived Context Summary] + summary)
          + history + user(当前)]
        （build_messages 不把预存的 user 算进 history，依赖 ④ 的先执行）

SAVE (loop.py:276-293):
  ⑤ skip = 2 + len(ctx.history)                ★原 1+len(history)
     result.messages = [system, *history, user(当前), assistant回复, tool结果...]
     skip 跳过 system + history + 已预存的 user → import 从 assistant 回复开始，不重复
  ⑥ _clear_pending_user_turn(session)          ★正常完成 → 消费标记
  ⑦ enforce_file_cap(on_archive=lambda chunk: memory.raw_archive(chunk, session_key)):
     len(messages) > 2000 → retain_recent_legal_suffix(2000)
       → dropped[already_consolidated_count:] 交给 raw_archive（已合并前缀不入归档）★新增
  ⑧ sessions.save(session)                      ← 图 1 存储层
  ⑨ 后台 maybe_consolidate_by_tokens（不阻塞响应）
```

---

## 图 3：Runner 迭代循环（runner.py，与 step18 相同，仅 import 路径变更）

```
AgentRunner.run(spec)
   ├─ RequestContext(session_key) → ContextVar 绑定 → hook.before_run
   ▼
   _run_loop for iteration in range(max_iterations):
     ① governance.prepare_for_model(messages)      ② tools.get_definitions()
     ③ provider.chat_stream_with_retry(messages) → LLMResponse
     ④ finish_reason 分支: error / tool_calls(图4) / 空内容重试 / length 继续 / 正常内容
     ⑤ injected 回调合并  ⑥ goal_active → 继续目标消息
   ▼
   AgentRunResult(final_content, messages, tools_used, usage, stop_reason)
   ▼ finally: reset_request_context（工具内 current_request_session_key 失效）
```

---

## 图 4：工具调用链（与 step18 完全相同，prepare_call 六步）

```
response.tool_calls → 分组 ≤10 并发 → _run_tool
   → prepare_call(name, arguments): 解析→展开→cast→Schema 校验
   → tool.execute(**params)
        ├─ create/update_goal: current_request_context().session_key → sessions.get_or_create
        │    → metadata[goal_state] 读写（长期目标）
        └─ spawn: subagent 子链（同 runner）
   → normalize_tool_result → messages 追加 {"role":"tool", tool_call_id, name, content}
   → continue 下一轮迭代
```

---

## 图 5：AutoCompact 后台压缩链（★全新，autocompact.py + consolidation.py:228-279）

```
触发: run() 每条消息到达时同步检查 (loop.py:143-147)
      （step18 无此机制；nanobot 原版是 wait_for 1s 轮询，本步用户决策仅消息触发）
      schedule_background = asyncio.create_task  |  active = set(_pending_queues) 在飞会话
   │
   ▼
auto_compact.check_expired(...)                    (autocompact.py:73-91)
   ├─ sessions.list_sessions()                     ★新增 (session.py:460-509)
   │     glob(*.jsonl) → _decode_storage_key → metadata 行 + 首条 user preview
   │     → 按 updated_at 降序
   ├─ 跳过: 空 key / "dream:" 内部会话 / _archiving 中 / active 会话
   ├─ _is_expired(updated_at, ttl): ttl<=0 → False（默认禁用） (autocompact.py:31-43)
   └─ _has_compactable_idle_tail(key):            (autocompact.py:45-63)
         tail = messages[last_consolidated:]
         probe = Session 副本(只含 tail, lc=0)     ★不碰真实 session
         probe.retain_recent_legal_suffix(8, extend_to_user=True)
         dropped 非空 → 值得压缩
      → _archiving.add(key) → schedule_background(_archive)（防重复调度）
   ▼
_archive(key, runtime)                             (autocompact.py:93-120)
   ▼
consolidator.compact_idle_session(key, max_suffix=8)   (consolidation.py:228-279)
   ├─ get_lock(session_key) 锁（与回合内 maybe_consolidate 互斥）
   ├─ sessions.invalidate(key) → get_or_create()  ★强制从磁盘重载
   ├─ messages_to_summarize = messages[last_consolidated:]
   ├─ probe 副本 retain_recent_legal_suffix(8, extend_to_user=True)
   │     → messages_to_keep（保留最近 8 条并回溯到 user turn）
   │     → messages_to_remove（= dropped[already_consolidated_count:]）
   ├─ archive(removed, summary_messages=全部): LLM 摘要
   │     → store.append_history(摘要)  失败 → store.raw_archive(原文)
   ├─ 成功 → session.metadata["_last_summary"] = {text, last_active}
   ├─ session.messages = keep; last_consolidated = 0; save
   ▼
_archive 读回 metadata._last_summary
   → self._summaries[key] = (text, last_active)    ★内存热缓存（进程未重启）
   → finally: _archiving.discard(key)
   ▼
下次该会话回合 COMPACT 时 → prepare_session（图 6）注入摘要
```

---

## 图 6：摘要数据流（_last_summary 的产生 → 注入，★全新）

```
                          ┌──────────────────────────────┐
                          │ 产生（两条路径，都写 metadata）│
                          │ ① 回合内 maybe_consolidate   │
                          │    (consolidation.py:225)    │
                          │ ② 后台 compact_idle_session  │
                          │    (consolidation.py:270)    │
                          └──────────────┬───────────────┘
                                         │ metadata["_last_summary"]
                                         │   = {"text": ..., "last_active": ...}
                                         ▼
prepare_session(session, key)      (autocompact.py:122-143)
   │  dream: 直通 (None)
   │  key in _archiving 或 _is_expired → get_or_create 强制重载 ★拿到压缩后的最新数据
   ├─ 热路径: self._summaries.pop(key)     ← 进程未重启, 内存缓存命中
   │    → "Previous conversation summary (last active {t}):\n{text}"
   ├─ 冷路径: metadata["_last_summary"]    ← 进程重启后从磁盘恢复
   │    → 同上格式化
   └─ 都无 → (session, None) → _state_compact 兜底再读一次 metadata
   ▼
ctx.summary（只消费一次，pop 即用）
   ▼
build_messages(session_summary=...) → system prompt 的 [Archived Context Summary] 段
   ▼
LLM 在回合开始时即拥有压缩前的历史摘要
```

---

## 图 7：辅助链（★全新/变更）

```
① /new 重置 (main.py:121-127)
   invalidate(key)          ★清 hot + overflow 两级缓存 (session.py:400-403)
   _get_session_path(key)   ★base64url 路径
   path.unlink()            ★删除磁盘文件
   ⇒ 旧对象即使被在飞任务强引用也回不到缓存（对比 step18 _cache.pop 漏清 overflow）

② fork_session_before_user_index(source, target, n)  (session.py:405-458)  ★新增
   遍历 source.messages，遇第 n+1 条 user 消息即停，之前消息 deepcopy
   metadata 剥离 volatile keys（goal_state/pending_user_turn/_goal_continuation_rounds）
   last_consolidated 钳制: 越界 → 丢 _last_summary, lc=0
   save(target, fsync=True)

③ list_sessions()  (session.py:460-509)  ★新增（AutoCompact / HTTP API 前置）
   glob → base64 解码（失败 fallback lossy）→ metadata + preview → updated_at 降序
```

---

## 图 8：关键数据结构流（★ = 变更）

```
InboundMessage(content, chat_id, session_key)
   ▼ TurnContext(msg, session_key, state, session, ★summary(pending/兜底),
     history, initial_messages, result, outbound)
   ▼ AgentRunSpec(initial_messages, tools=ToolRegistry, provider, max_iterations,
     hook, session_key, goal_active_predicate, goal_continuation_rounds)
   ▼ messages: list[dict]（对话状态唯一载体，结构同 step18）
   ▼ LLMResponse(content, tool_calls, finish_reason, usage)
   ▼ AgentRunResult(final_content, messages, tools_used, usage, stop_reason)
   ▼ OutboundMessage(content, metadata={stop_reason, tokens})
   ▼ Session (JSONL 持久化):
       首行 metadata: key, created_at, updated_at,
                     metadata{ ★pending_user_turn: bool,
                               goal_state, _goal_continuation_rounds,
                               ★_last_summary: {text, last_active} },
                     ★last_consolidated（__post_init__ 钳制）
       其余行: 消息 JSON（含 role/content/timestamp/name/tool_call_id/tool_calls）
       文件名: ★base64url(key).jsonl
```

---

## 图 9：阅读路径建议（按数据流走一遍）

```
图 2 (回合状态机)      ← 先看 ★ 标注的 6 处插入点，与 step18 状态机 diff
  │
  ▼ RESTORE/BUILD/SAVE 时
图 1 (会话存储层)       ← 两级缓存 + base64url + 迁移，回答"Session 从哪来去哪"
  │
  ▼ COMPACT 时
图 6 (摘要数据流)       ← prepare_session 热/冷双路径 → summary 注入
  │
  ▼ 消息到达时（图 0 顶部触发点）
图 5 (AutoCompact 链)   ← check_expired → _archive → compact_idle_session → _last_summary
  │
  ▼ 随时
图 7 (辅助链)           ← /new / fork / list_sessions
  ▼
图 8 (数据结构)         ← 任意时刻回看当前环节的数据形状

核心一句话：step18 是"所有智能发生在 messages 列表上"；
step19 在 messages 的上下两端各加了一道闸——
上端（BUILD）预存 user 消息 + pending 标记（崩溃恢复），
下端（SAVE）skip=2 去重 + enforce_file_cap 上限 + clear 标记；
并新增一条完全独立的后台链 AutoCompact，把闲置会话在"用户回来之前"压缩成摘要，
通过 prepare_session 在回合开始前把摘要送进 system prompt，
让压缩对用户完全透明。
```
