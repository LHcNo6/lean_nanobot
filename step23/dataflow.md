# Step 23 — Data Flow (Mid-turn Injection + Subagent System Channel)

step23 新增/变更的链条：① spawn 链（session 归属传播，从 loop 一路带到 announce）② 注入链（announce → 队列 → 回调 → runner，**回环进当前 turn**）③ system 独立 turn 链（锁空闲时的双路径）。回合状态机 8 态、出站链、provider 链（step21/22 成果）零改动。

---

## 图 0：全局视图（三条新链 + 不变链）

```
   ┌────────────────────── 回合编排链 (step21/22 原样，8 态) ──────────────────────┐
   │  RESTORE→COMPACT→COMMAND→BUILD→RUN→SAVE→RESPOND→DONE                           │
   │                                                                                │
 ②注入链(核心新增)                     ①spawn 链(session 归属传播)       ③system 独立 turn 链
 subagent._announce                    _state_run → _build_agent_spec     (锁空闲时)
  → bus.publish_inbound                 spec.session_key                  system 消息 → _process_system_message
  → loop.run 消费                        → runner.run bind               (loop.py:237-240 分叉)
  → _dispatch 分流 ★                    RequestContext(session_key)      ├─ _persist_subagent_followup
  ├─ 锁占用 → pending_queue ★          (runner.py:96-97)                 │   前置持久化+去重(422-447)
  │    → _drain_pending 阻塞等待         → SpawnTool.execute               ├─ current_role="assistant"(477)
  │    → runner.injection_callback       current_request_context()        ├─ build_messages merge(484-490)
  │    → 子代理结果注入进当前 turn       (spawn.py:46-50)                  └─ 跑 runner(带注入回调)
  └─ 锁未占用 → ③ 独立 turn             → spawn(session_key=)              │
                                        → _session_tasks 跟踪(100-101)     └─ 与②共用同一持久化标记
  子代理跑完 → _announce                 → _run_subagent → _announce(override=session_key)   (双路径并存)
   → 回 bus.inbound(回环)
   → 锁仍被主 turn 占着 → ② 进队列 ★
```

---

## 图 1：spawn 链 — session 归属从 loop 传播到 announce（★全新）

```
状态机 RUN 态 / system 专线
   │ _build_agent_spec(session_key=session_key)               (loop.py:336)
   ▼
AgentRunSpec.session_key
   │ runner.run() 开头:
   │   req_ctx = RequestContext(session_key=spec.session_key)  (runner.py:96)
   │   bind_request_context(req_ctx)                           (runner.py:97)
   ▼
工具执行期间（同 task 块内）
   SpawnTool.execute()
   │   req = current_request_context()                         (spawn.py:46)
   │   session_key = req.session_key if req else None          (spawn.py:47)
   ▼
SubagentManager.spawn(task, label, session_key=session_key)    (subagent.py:71-78)
   ├─ 并发上限检查 get_running_count() >= 5 → 拒绝             (79-84)
   ├─ 生成 task_id、登记 _task_statuses                        (85-94)
   ├─ origin = {channel, chat_id, session_key}                 (96)
   ├─ create_task(_run_subagent(...))                         (98)
    └─ _session_tasks[session_key].add(task_id)   ★session 归属  (100-101)
        │   done_callback → _cleanup       (103-111)
        ▼
   _run_subagent → runner.run(AgentRunSpec(子任务))             (114-136)
        ▼ 完成/失败
   _announce(task_id, label, task, result, origin, status)     (138-155)
        ├─ content = "[Subagent '...' completed]\n\nTask:...\n\nResult:..." (140-144)
        ├─ override = origin.session_key or f"{channel}:{chat_id}"  ★(145)
        ├─ metadata = {"injected_event":"subagent_result",
        │              "subagent_task_id": task_id}  ★去重/识别标记 (146)
        └─ InboundMessage(channel="system", sender_id="subagent",
                          chat_id=override, session_key_override=override, ...) (147-154)
   ▼
bus.publish_inbound(msg) → 回到 loop.run 消费（图 2）
★step22 缺陷: spawn 硬编码 origin_channel="cli", origin_chat_id="direct"，
  回包路由到错误 session（session_key 永不传）；step23 补全。
```

---

## 图 2：注入链全景（★核心，announce → 当前 turn 的消息序列）

```
subagent._announce (图 1 底部)
   ▼ bus.publish_inbound (subagent.py:155)
loop.run() 主循环消费 (loop.py:176-185)
   ▼ create_task(self._dispatch(msg)) (185)
_dispatch(msg) (loop.py:195-227)
   │ session_key = msg.session_key_override or msg.session_key or msg.chat_id (196)
   │ lock = self._session_locks.setdefault(...)       (197)
   ▼ 分流（图 3）
   ├─ 锁正被主 turn 占用 → await queue.put(msg)  ★不再竞争成独立 turn (198-202)
   │     ▼ 当前 turn 的 runner 正在 _drain_pending 里阻塞等待
   │     queue 拿到消息 → items 收集 (393-398)
   │     ▼ 注入进 messages（图 4/图 5）→ 下一轮迭代 → 模型看到子代理结果并总结
   │     ▼ 最终 result.messages 经 _state_save import_messages 持久化     (534-551)
   │        （标记随 user 消息落库，图 9）
   └─ 锁空闲 → _process_message → channel=="system" → 图 6 独立 turn (237-240)
★核心设计：「队列归当前 turn 所有」——锁占用 ≠ 排队等下一 turn，
  而是等本 turn 注入。这是 step23 解决「并发错乱/回包插队」的根本。
```

---

## 图 3：_dispatch 锁语义与队列生命周期（★重写，loop.py:195-227）

```
_dispatch(msg)
   │
   ├─ lock.locked()?  ──是──► _get_or_create_queue(session_key).put(msg)  (198-202)
   │                          return（本消息成为注入源）
   │
   └─ 否（持锁）:
        async with lock:                                       (205-211)
           pending = _get_or_create_queue(session_key)  ★持锁注册队列 (206)
           response = await _process_message(msg, session_key,
                       pending_queue=pending, runtime=self.runtime)     (207-209)
           if response: bus.publish_outbound(response)                 (210-211)
        finally:                                                     (212-227)
           if self._pending_queues.get(session_key) is pending:
               queue = pop(session_key)        ★identity 判空后弹栈      (217-218)
           else:
               queue = pending                                       (219-220)
           while queue 非空:
                 item = queue.get_nowait()
                 bus.publish_inbound(item)     ★剩余 re-publish        (221-227)
   ▼
★变化 vs step22:
   - 旧 `_drain_leftover`（把剩余消息排队成独立 turn）删除
   - 弹栈必须 identity 判空：并发下新 _dispatch 可能已注册新队列，不能误弹
   - re-publish ≠ 丢弃：注入窗口关闭后才到达的消息，下一轮当普通消息处理（不静默丢失）
   ✓ 锁空闲时消息直接处理（不走队列），锁占用时注入
★测试锚点: test_dispatch_leftover_republishes_to_bus (test.py:1424)
   test_dispatch_pending_queue_registered_for_turn (test.py:1435)
```

---

## 图 4：阻塞等待时序 — turn 存活等子代理（★全新，loop.py:387-418）

```
主 turn runner 迭代                                子代理(独立 task)
   │                                                │
   │  final 响应后                                  │
   │  _try_drain_injections (图 5)                  │
   │  → _drain_injections → _drain_pending          │
   │    ├─ get_nowait 排干现有队列 (393-398)          │
   │    ├─ 空 且 get_running_count_by_session>0      │
   │    │      (401-405)                            │
   │    └─ await wait_for(queue.get(), 300)         │
   │         (408) ★阻塞，turn 保持存活               │
   │         │                                      │
   │         │                               _run_subagent 完成
   │         │                               → _announce → bus.publish_inbound
   │         │                               → loop.run → _dispatch
   │         │                               → 锁被主 turn 占 → queue.put ★
   │         │                                      │
   │         ◄────── queue.get() 返回 (409-411) ─────┘
   │    └─ 继续排干到 limit (412-417)
   │    → wrap 成 user 消息 → injection_cycles+1 → 下一轮迭代 (图 5 循环)
   │
   └─ 300s 超时 → return [] (409-410) → 本轮结束，不报错
★风险: 子代理异常不再 announce → 拖满 300s 后继续（step23.md 取舍表）
★e2e 时序技巧: 用延迟子代理(_SlowSubProvider 0.1s) + 轮询 watcher 观测
                `_session_tasks` 运行窗口，避免瞬时完成错过断言
   (test.py:1443-1502)
```

---

## 图 5：runner 注入决策树（★升级，runner.py:258-392）

```
final 响应后 (514-518) / 工具执行后 (470-473)
   ▼ _try_drain_injections(spec, messages, assistant_msg, cycles,
      phase, allow_goal_continue)                      (268-318)
   │
   ├─ injection_cycles < _MAX_INJECTION_CYCLES(5)?     (291)
   │     └─ _drain_injections(spec)  (320-368)
   │          ├─ 回调签名探测 limit 参数       ★inspect.signature 兼容旧回调 (330-340)
   │          │    → callback(limit=3) 或 callback()
   │          ├─ 异常防护 → logger.exception + []        (341-343)
   │          ├─ 逐条过滤 _has_injection_content        (344-366, 370-378)
   │          │    None / 空白串 / 空列表 → 丢弃
   │          │    dict 需 role=="user"+content → 保留
   │          │    其他对象 → wrap 成 user 消息
   │          ├─ 超 _MAX_INJECTIONS_PER_TURN(3) → 截断+warning  (360-366)
   │
   ├─ 有注入 → real_injection=True, cycles += 1        (309-310)
   │
   ├─ 无注入 and allow_goal_continue and
   │     goal_active and rounds < 12
   │     → 降级为 goal 续跑消息 ★不占 injection cycle (299-305, 258-266)
   │
   ├─ 仍无 → return (False, cycles) → turn 结束      (306-308)
   │
   ├─ assistant_message 延迟追加★ 只有 turn 真继续才 append (311-312)
   │     （对比 step22: 先 append 再判 goal → 幽灵 assistant 消息）
   ├─ messages += 注入 user 消息 (313-314)
   └─ return (True, cycles) → 外层 continue
★error 路径不注入 (429-436) ★保留 step21 测试契约
   （偏离 nanobot: nanobot 在 error 路径也会 drain，step30 错误语义收敛时对齐）
★测试锚点: TestRunnerInjectionUpgrade (test.py:1530)
   - test_try_drain_injections_injection_precedes_goal
   - test_try_drain_injections_goal_cap / goal_continue
```

---

## 图 6：system 独立 turn 链（★全新，loop.py:449-515，锁空闲路径）

```
_dispatch 持锁 → _process_message (229-267)
   │ msg.channel == "system" ?                          (237-240)
   ▼
_process_system_message(msg, runtime, pending_queue)   (449-515)
   ├─ 解析 channel/chat_id + key                        (462-466)
   ├─ session = get_or_create(key)                       (468)
   ├─ _restore_pending_user_turn(session)  ★崩溃恢复雏形(图 8)  (469-470)
   ├─ auto_compact.prepare_session + maybe_consolidate   (471-472)
   ├─ is_subagent = sender_id=="subagent"               (474)
   ├─ _persist_subagent_followup(session, msg)  ★前置持久化 (475-476)
   │     ├─ 同 task_id 已落库 → 去重跳过 (434-439)
   │     └─ add_message("assistant", content,           (440-446)
   │           sender_id, injected_event="subagent_result",
   │           subagent_task_id) → save
   ├─ current_role = "assistant" if is_subagent else "user"  (477)
   ├─ build_messages(current_message="", history, ...,   (484-490)
   │     current_role)  ★角色交替 merge(图 7)
   ├─ spec = _build_agent_spec(msg, key, session,       (492-495)
   │     initial_messages,
   │     injection_callback=_build_injection_callback(pending_queue,key,session))
   ├─ result = runner.run(spec) ★同样可注入(子代理再出子代理)   (496)
   ├─ session.import_messages(result.messages[skip:])    (498-499)
   ├─ save + 后台 consolidate                             (500-507)
   └─ 返回 OutboundMessage(content=final, chat_id=chat_id,  (509-515)
             metadata={"stop_reason": ...})
★与状态机路径对照: 跳过 COMMAND 检查（系统消息不进命令路由）
★双路径并存(取舍): 锁空闲→这里(前置持久化 + 独立 answer)；
  锁占用→图 2(仅标记注入，不做前置持久化)——两条路径共用图 9 同一定位标记
★e2e: 子代理 e2e 断言「0.4s 内无第二个最终响应」证明注入同 turn
   test_subagent_announce_injected_mid_turn (test.py:1443-1502)
```

---

## 图 7：角色交替与持久化标记（context.py:104-126 + loop.py:422-447）

```
持久化侧（system 路径）
   _persist_subagent_followup(session, msg)                (422-447)
      ├─ content 空 → False                                (430-431)
      └─ 同 subagent_task_id 已在 session.messages 里            (434-439)
           （判定 injected_event=="subagent_result" 且 task_id 相同）
         └─ 是 → False（去重，不重复注入）
         └─ 否 → add_message("assistant", ...,             (440-446)
                   injected_event="subagent_result",
                   subagent_task_id=task_id) → True

prompt 组装侧（build_messages, context.py:104-126）
   messages = [system] + history
   └─ 末尾 role == current_role ?
        ├─ 是:
        │   current_message 非空 → 拷贝 dict(last=dict(messages[-1]))
        │       ★不污染 session.messages 引用
        │     last["content"] = last.content + "\n" + current_message
        │     messages[-1] = last                          (113-121)
        │   current_message=="" → 直接 return（无空占位）  (122-123)
        └─ 否: messages.append({"role": current_role,
                "content": current_message})               (125-126)

场景 1 (system 路径): 末尾=刚持久化的 assistant(子代理结果)
   current_role="assistant" + current_message="" → 直接返回，不产生空占位 ★(122-123)
场景 2 (状态机路径): 末尾=user 消息
   current_role="user" + content → merge 进末尾（防 user+user 连续）
★动机: 子代理回包持久化后末尾是 assistant，再拼 user 消息会连续同角色，
   被部分 provider 拒绝——这是 step23 解决的第四类问题
★测试锚点: test_current_role_defaults_to_user / test_current_role_assistant
```

---

## 图 8：崩溃恢复雏形（★loop.py:566-588，step24 A5 checkpoint 铺垫）

```
mark 写入: _state_build (loop.py:314) → _mark_pending_user_turn (566-567)
mark 清除: _state_save (539) / _process_system_message (500)

_restore_pending_user_turn(session)                         (572-588)
   │ metadata["pending_user_turn"]?  (574-575)
   ├─ 无 → False（正常路径，不改动）
   └─ 有:
       ├─ 历史末尾是 user 消息（turn 只落了用户输入就崩了） (577)
       │    └─ append {"role":"assistant",
       │                "content":"Error: Task interrupted before a response was generated.",
       │                "timestamp": iso}   ★补 assistant → 角色交替合法 (578-584)
       ├─ 清掉标记 _clear_pending_user_turn               (585-587)
       └─ return True → 触发 save (272, 470)
★调用点: _state_restore (loop.py:571-572) + _process_system_message (469-470)
★局限: 只处理「user 后崩溃」；「assistant/tool 中途崩溃」未兜 → step24 正式 checkpoint
```

---

## 图 9：数据标记流（injected_event / subagent_task_id 生命周期）

```
产生点   subagent._announce metadata                   (subagent.py:146)
   │
   ├─ 注入路径（图 2）:
   │   pending InboundMessage
   │   → _pending_to_user_message (loop.py:367-379)
   │       ├─ sender=="subagent" + injected_event=="subagent_result"
   │       │   → row["injected_event"], row["subagent_task_id"]
   │       └─ 保留为 user 消息
   │   → 进入 runner messages → result.messages
   │   → _state_save import_messages 落库（带标记） (534-549)
   │
   └─ system 路径（图 6）:
       _persist_subagent_followup → assistant 消息落库（带标记）(440-446)
       → 后续同 task_id 消息 → 去重 (434-439)
   │
   ▼ 消费点
   历史中带 subagent_task_id 的消息 = 「这是子代理结果」
   → step29 (A12 HIDDEN_HISTORY_META 隐藏) 的识别基础
★去重范围: 仅同一 session；跨 session 幂等靠 pending 队列单次消费
★「只持久化标记，不隐藏」: 双路径都能看到子代理原始回包（取舍表）
```

---

## 图 10：阅读路径建议

```
图 1 (spawn 链)     ← session 归属怎么从 loop → spec → RequestContext →
                      SpawnTool → SubagentManager → announce 一路传下去
                      （先理解「消息从哪来、归属哪个 session」）
  │
  ▼
图 2 (注入链全景)    ← announce 回包怎么被「回环」进当前 turn——
                      bus → dispatch → 队列 → 回调 → runner 消息序列
            （核心心智模型: 队列归当前 turn 所有）
  │
  ▼ 拆开注入链的每一步
图 3 (dispatch 锁)  ← 为什么「锁占用=等注入」而不是排队成独立 turn；
                      弹栈 identity 判空 / re-publish 不丢消息
图 4 (阻塞时序)     ← turn 存活等子代理的 300s 等待窗口
图 5 (runner 决策)  ← 注入 vs goal-continue 的优先级; 每轮两种时机
  │
  ▼ 锁空闲的另一条路
图 6 (system 独立)  ← 不经过 8 态，独立生命周期; 与图2 共用标记
                        │
                        ▼ 实现依赖
图 7 (角色交替+去重) ← 前置持久化 assistant + build_messages merge
                       （同角色连续被 provider 拒的解法）
  │
  ▼ 附属能力
图 8 (崩溃恢复)     ← pending_user_turn 标记的补洞雏形
图 9 (标记流)       ← subagent_task_id 从产生→消费→去重→step29 基础
  ▼
结论一句话: step23 的注入链本质是「把子 agent 的结果消息重新路由进
当前 turn 的 LLM 上下文」，而队列、锁、标记三件套保证三次路径
（锁空闲独立 / 锁占用注入 / 崩溃恢复）不会互相干扰。

下一步 step24: 把图 8 的简版 `_restore_pending_user_turn` 升级为
完整 `_set/_restore_runtime_checkpoint`（assistant/tool 中途崩溃也可恢复）
+ `_save_turn` 持久化净化（丢空 assistant / 孤儿 tool result / 超长截断）。

附带阅读: todolist.md A2/A3/A6 状态表（step23 完成行）；step23.md 四节取舍表（error 回退、300s、双路径）。
```

---

*图中的英文词条为 step23 工程内英文常量/标识，行号以 step23 源码实测为准。*