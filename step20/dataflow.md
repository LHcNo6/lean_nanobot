## Step 20 — Data Flow (Channel Framework)

相对 step19 新增/变更的四条链：
① 入站链：通道捕获 → 权限判定 → 总线 → agent（回合主链前端全部重写）
② 出站链：agent → dispatcher 路由 → 通道投递（普通重试 + 流式直投，★全新）
③ 配对授权链：陌生人 DM → 配对码 → 审批 → 白名单（★全新）
④ 通道生命周期链：发现/装配/启动/停止（★全新）

---

## 图 0：全局视图（四条链）

```
   ┌─────────────────────────── 回合编排链 (AgentLoop, step19 原样) ───────────────────────────┐
   │            RESTORE→COMPACT→BUILD→RUN→SAVE→RESPOND→DONE                                    │
   │                                                                                           │
 ①入站链                         ②出站链                       ③配对链                 ④生命周期链
 用户──►CliChannel.start     manager._dispatch_outbound   CliChannel(拒绝分支)   manager._init_channels
      │  ainput                │  按 msg.channel 路由         generate_code      │  pkgutil 发现
      │  _handle_message      │  ├─StreamDelta→send_delta  ──►approve_code      │  load_channel_class
      │  is_allowed(三级)      │  └─Outbound──►_send_with_retry ──►is_approved   │  实例化+注入
      │  bus.publish_inbound   │       │                        ▲                │  start_all/stop_all
      ▼                        ▼       ▼                        │                ▼
  ┌──────────┐           ┌──────────────┐              ┌───────────────┐   ┌────────────────┐
  │ MessageBus│◄──────────►│ AgentLoop    │              │ pairing.json  │   │ ChannelManager │
  │ inbound/  │           │ run():_dispatch│             │ approved/pending│ │ (dispatcher+  │
  │ outbound  │           │ → _process    │             └───────────────┘  │  retry+start)  │
  └──────────┘           └──────────────┘              （CLI 直通不触发）  └────────────────┘
```

---

## 图 1：通道装配期（main.py:112-121 + manager.py:41-68，★全新）

```
main.py 组装（组合根）:
   PairingStore(path="pairing.json")                      (main.py:77)
   on_command(text)->bool: /dream /history /new           (main.py:79-110, 返回True=消费)
   ChannelManager(config={"cli":{enabled,allow_from,streaming}}, bus=bus, pairing, on_command)
                                                            (main.py:112-117)
   cli_channel.chat_id = session_key                       (main.py:119-121) ★通道ID↔会话key打通
          │
          ▼
manager._init_channels()                                  (manager.py:41-68)
   ├─ names = discover_channel_names()                    ★零导入扫描 (registry.py:16-22)
   │     pkgutil.iter_modules(step20.channels.__path__)
   │     → 过滤 _INTERNAL{"base","manager","registry"} / "_" 前缀 / 子包 → ["cli"]
   ├─ candidates = names ∪ config键（extra channels 预留）
   ├─ enabled 判定: section.enabled 默认= name in DEFAULT_ENABLED_CHANNELS={"cli"}
   ├─ load_channel_class(name)                            (registry.py:25-33)
   │     延迟 import step20.channels.cli → dir() 扫描第一个 BaseChannel 子类
   │     失败/不存在/初始化异常 → print 警告 continue     ★三级降级，坏通道不拖垮
   ├─ channel = CliChannel(section, bus, pairing=pairing) ★统一注入同一 bus/pairing
   ├─ on_command 注入: hasattr(channel,"on_command") 时赋值 (manager.py:65-66)
   └─ channels["cli"] = channel → print "[manager] CLI channel enabled"
```

---

## 图 2：入站链（★全新，cli.py:43-56 → channel.py:82-127 → loop.py:158-168）

```
CliChannel.start()  while self._running                        (cli.py:43-56)
   │  text = await ainput("You: ")      ★run_in_executor 线程池 (cli.py:10-12)
   ├─ 空输入 → continue
   ├─ "/exit" → await self.stop() → break            ★通道原生，不走回调
   ├─ on_command(text) 返回 True → continue          ★/dream /history /new 被消费
   └─ 普通文本:
        self._turn_done.clear()                       ★回合信号清零 (cli.py:54)
        ▼
        await self._handle_message("user", chat_id=session_key, text)
           │                                           (channel.py:82-127)
           ▼
        is_allowed(sender_id)                         (channel.py:73-80)
           ├─ allow_from ["*"] → True   ★CLI 直通
           ├─ allowFrom 精确匹配 → True
           └─ pairing.is_approved("cli", sender)      ★第3级，CLI 走不到
           │
           ├─ False → 拒绝分支 → 见 图 3（配对链）
           │
           └─ True 放行:
                meta = dict(metadata or {})
                if supports_streaming: meta["_wants_stream"]=True   (channel.py:114-116)
                   ★双重判定: config.streaming=True 且 子类覆写 send_delta (channel.py:68-71)
                InboundMessage(channel="cli", sender_id, chat_id,
                               content, media=[], metadata, session_key_override)   (events.py:8-18)
                await bus.publish_inbound(msg)
   ▼
AgentLoop.run(): msg = await bus.consume_inbound()            (loop.py:139-148)
   ├─ auto_compact.check_expired(...)（step19 行为保留）
   └─ asyncio.create_task(_dispatch(msg))
   ▼
_dispatch(msg)                                                (loop.py:158-168)
   ├─ session_key = msg.session_key_override or msg.session_key or msg.chat_id  (loop.py:159)
   │     ★channel → session 打通：CLI 的 chat_id 就是会话 key
   ├─ 会话锁已占用 → 入 _pending_queues 排队（step19 行为保留）
   └─ async with lock: _process_message(msg, session_key)
        → RESTORE→COMPACT→BUILD→RUN→SAVE→RESPOND→DONE       ★step19 状态机原样
        → ctx.outbound（OutboundMessage）
        → await bus.publish_outbound(response)               (loop.py:167)
```

---

## 图 3：配对授权链（★全新，channel.py:93-112 + pairing.py 全链）

```
拒绝分支（is_allowed=False）:
   │
   ├─ is_dm=True（私聊场景）:
   │    code = pairing.generate_code("cli", sender_id)        (pairing.py:63-77)
   │       │  secrets.choice(36字符集) × 8 → "ABCD-EFGH"
   │       │  pending[code] = {channel, sender_id, created_at, expires_at=+600s}
   │       │  惰性 _gc_pending 清过期码 → _save（tmp+fsync+os.replace 原子写）
   │       ▼
   │    send(OutboundMessage(content=format_pairing_reply(code),
   │                         metadata={PAIRING_CODE_META_KEY: code}))   (channel.py:96-103)
   │       ▼ 用户收到"配对码 ABCD-EFGH，请 owner 批准"
   │
   └─ is_dm=False（群聊）→ 仅 print 警告，不回复          (channel.py:107-111)

owner 侧（CLI 控制台输入）:
   /pairing approve ABCD-EFGH ──► handle_pairing_command("cli", "approve ABCD-EFGH")
                                      │                    (pairing.py:180-225)
                                      ├─ list   → 列出全部 pending 码
                                      ├─ approve→ approve_code(code) (pairing.py:79-92)
                                      │     pending.pop(code) ★一次性，杜绝重放
                                      │     approved["cli"].add(sender_id) → _save
                                      │     → 返回 (channel, sender_id)
                                      ├─ deny   → deny_code(code)
                                      └─ revoke → revoke/revoke_channel（支持 2/3 参数形态）
   ▼ 之后
is_approved("cli", sender_id) → True → 放行                 (pairing.py:106-111)
   ★持久化于 pairing.json：重启后审批状态仍在
```

---

## 图 4：出站链 — 普通消息（★全新，manager.py:118-166 → cli.py:61-68）

```
AgentLoop._state_respond → bus.publish_outbound(OutboundMessage)   (loop.py:167)
   ▼
manager._dispatch_outbound()  while True                        (manager.py:118-141)
   │  msg = await wait_for(consume_outbound(), 1.0)   ★1秒轮询，可被 cancel 中断
   ├─ TimeoutError → continue
   ├─ CancelledError → break（stop_all 触发）
   ├─ channel = self.channels.get(msg.channel)
   │     ├─ None → print "[manager] Unknown channel" → continue
   │     ├─ isinstance(msg, StreamDeltaEvent) → 图 5（流式）
   │     └─ 普通 OutboundMessage → _send_with_retry(channel, msg)   (manager.py:143-166)
   ▼
_send_with_retry  ★指数退避:
   attempt 1:  await channel.send(msg)
      ├─ 成功 → return
      ├─ CancelledError → raise（向上穿透）
      └─ 失败 → 等 1s 重试 → 2s → 4s → 第3次失败 → print 警告后放弃
   ▼
CliChannel.send(msg)                                           (cli.py:61-68)
   print(f"\n[{stop_reason}]")  ★复刻 step19 main.py 输出格式
   print(content); print(f"  tokens: {tokens}")
   self._turn_done.set()        ★回合推进信号（唯一在 send）
   ▼ 阻塞在 start() 里的 await self._turn_done.wait() 解除
   打印下一个 "You: "（先响应后提示 UX）
```

---

## 图 5：流式链（★全新，channel.py:114-116 → loop.py:49-72/248 → runner.py:157-162 → manager.py:127-135 → cli.py:70-89）

```
放行时: meta["_wants_stream"] = True（通道声明流式意图）      (channel.py:116)
   ▼ ★当前 loop 始终附加流式 hook（_wants_stream 为向前兼容的声明，测试断言其存在）
_state_run: hooks.append(StreamPublishingHook(bus, chat_id=msg.chat_id,
                                              channel=msg.channel, session_key))  (loop.py:248-251)
   ▼
AgentRunner.run(spec):  wants_streaming = hook.wants_streaming()  (runner.py:157)
   │  True → provider.chat_stream_with_retry（外层超时放宽 2×）
   │  每个文本增量: iter_ctx.stream_content += text
   │                await hook.on_stream(iter_ctx, text)         (runner.py:161-162)
   ▼
StreamPublishingHook.on_stream(delta):                            (loop.py:57-63)
   → bus.publish_outbound(StreamDeltaEvent(content=delta, channel, chat_id,
                                           finished=False, session_key))
   ▼（回合结束/异常/空内容重试兜底）
on_stream_end: publish StreamDeltaEvent(content="", finished=True) (loop.py:65-69, runner.py:314/380)
   ▼
manager._dispatch_outbound:  isinstance(msg, StreamDeltaEvent)   (manager.py:127-135)
   ├─ finished=False → await channel.send_delta(chat_id, content) ★流式不重试（不可重放）
   └─ finished=True  → await channel.send_delta(chat_id, "", stream_end=True)  ★finished→stream_end 映射
   ▼
CliChannel.send_delta:                                            (cli.py:70-89)
   key = (chat_id, stream_id or "")
   ├─ 非结尾: self._buffers.setdefault(key, []).append(delta)    ★只累积不打印
   └─ stream_end=True: pop 缓冲（+尾部delta）→ "".join → print(full)
   ▼
用户看到：整段累积后一次打印；终响应 OutboundMessage 到达时
send() 打印 [stop_reason]/tokens + set _turn_done（流式回合的结束仍由终响应负责）
```

---

## 图 6：生命周期链（★全新，manager.py:92-116 + main.py:129-142）

```
main.py: loop_task = create_task(agent_loop.run())
         dream_task = create_task(_dream_loop(agent_loop))
         try: await manager.start_all()          ← 一行阻塞
         finally: agent_loop.stop(); cancel loop/dream; await manager.stop_all()
                                                                    (main.py:129-142)
   ▼
start_all()                                   (manager.py:92-106)
   ├─ 无通道 → print "No channels enabled" → return（幂等）
   ├─ self._dispatch_task = create_task(_dispatch_outbound())   ★dispatcher 先启
   ├─ tasks = [create_task(_start_channel("cli", channel))]     ★通道后启
   │     _start_channel: try await channel.start() 失败→print   (manager.py:70-74)
   └─ await gather(*tasks, return_exceptions=True)
        └─ CliChannel.start() 无限循环直到 /exit → stop() 置位 → while 退出 → start 返回
           ▼ gather 返回 → start_all 返回 → main 进入 finally
   ▼
stop_all()                                    (manager.py:108-116)
   ├─ _dispatch_task.cancel() + suppress(CancelledError)  ★先停 dispatcher（不再投递）
   └─ 逐个 _stop_channel: await channel.stop()（优雅）→ task.cancel() 兜底  (manager.py:76-90)
```

---

## 图 7：关键数据结构流（★ = 新增/变更）

```
InboundMessage(content, channel="cli", sender_id, chat_id, timestamp,
               session_key, session_key_override, ★media: list[str]=[], metadata)   (events.py:8-18)
   ▼ 权限放行时 metadata 含 ★_wants_stream: True
   ▼ TurnContext(msg, session_key, ...)（step19 原样）
   ▼ AgentRunSpec(..., hook=CompositeHook([...]+★StreamPublishingHook))
   ▼ LLMResponse / AgentRunResult（step19 原样）
   ▼ 出站两通道（★新分裂）:
        ├─ OutboundMessage(content, channel, chat_id, metadata{stop_reason,tokens})  → send（终响应，重试）
        └─ ★StreamDeltaEvent(OutboundMessage + finished: bool, session_key)
               → send_delta（流式，不重试）                                            (events.py:29-32)
   ▼
ChannelManager.channels: dict[str, BaseChannel]   ★name → 实例（唯一的路由表）
MessageBus: inbound/outbound 双队列               （step19 原样）
PairingStore 持久化 pairing.json:
   {"approved": {channel: [sender_ids]},
    "pending":  {code: {channel, sender_id, created_at, expires_at}}}
CliChannel._buffers: dict[(chat_id, stream_id), list[str]]   ★流式缓冲表
```

---

## 图 8：阅读路径建议（按数据流走一遍）

```
图 2 (入站链)        ← 从 CliChannel.start 的 ainput 开始，走到 bus 为止；
                       对比 step19 main.py REPL 的搬迁差异（/exit 原生 / on_command 回调）
  │
  ▼ 权限判定处
图 3 (配对链)        ← is_allowed 三级 → DM 拒绝分支 → generate_code→approve_code 闭环
  │
  ▼ agent 回复时
图 4 (出站链-普通)   ← _dispatch_outbound 路由 → _send_with_retry → send → _turn_done
  │
  ▼ 开 streaming 时
图 5 (流式链)        ← _wants_stream → StreamPublishingHook → StreamDeltaEvent → send_delta 缓冲
  │
  ▼ 程序启动/退出
图 6 (生命周期链)    ← start_all/stop_all 顺序与对称性
  ▼
图 1 (装配期)        ← 任何时刻回看"通道从哪来、注入什么"
  ▼
图 7 (数据结构)      ← 对照事件形状与缓冲表

核心一句话：step19 解决了"agent 内部回合的状态管理"，
step20 把系统的两端接上了真实世界——
前端（入站）由通道负责捕获与鉴权，后端（出站）由 dispatcher 负责路由与投递，
中间依然只有一个 MessageBus 和一条不变的回合计。
权限（is_allowed 三级）、流式（send_delta）、配对（PairingStore）
三道闸全部收敛在通道层，agent 对通道类型完全无感知。
```