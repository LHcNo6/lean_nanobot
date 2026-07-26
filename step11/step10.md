# Step 10 鈥?AgentLoop 鐘舵€佹満

## 鐩爣

灏?step9 涓唴鑱斿湪 `main.py` 鐨?`_agent_loop` 閲嶆瀯涓烘寮忕殑 `AgentLoop` 绫伙紝寮曞叆 6 鎬佺姸鎬佹満锛屽疄鐜?per-session 搴忓垪鍖栧拰璺?session 骞跺彂銆?

## 瑙ｅ喅鐨勯棶棰?

| 闂 | 瑙ｅ喅鏂瑰紡 |
|------|---------|
| `_agent_loop` 涓嶅彲澶嶇敤 | 鎻愬彇涓虹嫭绔?`loop.py`锛宍AgentLoop` 绫?|
| 鏃犵粨鏋勫寲鐘舵€?| 6 鎬佺姸鎬佹満 + 杞崲琛?`_TRANSITIONS` |
| 鍚?session 绔炴€?| `asyncio.Lock` 姣忎釜 session_key |
| 璺?session 骞跺彂 | `asyncio.create_task` 姣忎釜娑堟伅 |
| 閿欒瀵艰嚧杩涚▼姝?| 姣忎釜鐘舵€?handler 鐨勫紓甯歌 `_process_message` 鎹曡幏骞惰繑鍥?error outbound |
| 鍛戒护涓庡鐞嗚€﹀悎 | 鍛戒护鍦?CLI 灞傚鐞嗭紝鐘舵€佹満绾补 |

## 鏂板鏂囦欢

| 鏂囦欢 | 琛屾暟 | 鑱岃矗 |
|------|------|------|
| `loop.py` | 150 | `AgentLoop` 绫?+ `TurnState` 鏋氫妇 + `TurnContext` 鏁版嵁绫?|

## 淇敼鏂囦欢

| 鏂囦欢 | 鍙樺寲 |
|------|------|
| `main.py` | 浣跨敤 `AgentLoop(bus, ...)` 浠ｆ浛鍐呰仈 `_agent_loop` |
| `test.py` | 澧炲姞 14 涓?AgentLoop 娴嬭瘯锛屽叡 44 涓祴璇?|

## 6 鎬佺姸鎬佹満

```
RESTORE 鈫?COMPACT 鈫?BUILD 鈫?RUN 鈫?SAVE 鈫?RESPOND 鈫?DONE
```

### 杞崲琛?

```python
_TRANSITIONS = {
    (TurnState.RESTORE, "ok"): TurnState.COMPACT,
    (TurnState.COMPACT, "ok"): TurnState.BUILD,
    (TurnState.BUILD,   "ok"): TurnState.RUN,
    (TurnState.RUN,     "ok"): TurnState.SAVE,
    (TurnState.SAVE,    "ok"): TurnState.RESPOND,
    (TurnState.RESPOND, "ok"): TurnState.DONE,
}
```

### 鐘舵€佽亴璐?

| 鐘舵€?| Handler | 鎿嶄綔 |
|------|---------|------|
| RESTORE | `_state_restore` | `sessions.get_or_create(session_key)` |
| COMPACT | `_state_compact` | `consolidator.maybe_consolidate(session, max_tokens)` |
| BUILD | `_state_build` | `session.get_history()` + `context.build_messages()` |
| RUN | `_state_run` | `AgentRunner().run(spec)` |
| SAVE | `_state_save` | `session.import_messages()` + `sessions.save()` |
| RESPOND | `_state_respond` | `bus.publish_outbound(OutboundMessage(...))` |

### TurnContext

```python
@dataclass
class TurnContext:
    msg: InboundMessage
    session_key: str
    state: TurnState = TurnState.RESTORE
    session: Session | None = None
    summary: str | None = None
    history: list[dict] = field(default_factory=list)
    initial_messages: list[dict] = field(default_factory=list)
    result: AgentRunResult | None = None
    outbound: OutboundMessage | None = None
```

## 鏍稿績娴佺▼

### `run()` 鈥?涓诲惊鐜?

```python
async def run(self):
    self.running = True
    while self.running:
        msg = await self.bus.consume_inbound()
        asyncio.create_task(self._dispatch(msg))
```

### `_dispatch(msg)` 鈥?鍒嗗彂

```python
async def _dispatch(self, msg):
    session_key = msg.session_key or msg.chat_id
    lock = self._session_locks.setdefault(session_key, asyncio.Lock())
    async with lock:           # 鍚?session 涓茶
        response = await self._process_message(msg, session_key)
        if response is not None:
            await self.bus.publish_outbound(response)
```

### `_process_message(msg)` 鈥?鐘舵€佸紩鎿?

```python
async def _process_message(self, msg, session_key):
    ctx = TurnContext(msg=msg, session_key=session_key)
    while ctx.state != TurnState.DONE:
        handler = getattr(self, f"_state_{ctx.state.name.lower()}")
        try:
            event = await handler(ctx)
        except Exception as exc:
            # 閿欒鐘舵€?鈫?杩斿洖 error outbound
            ctx.outbound = OutboundMessage(content=f"Error: {exc}", ...)
            break
        next_state = self._TRANSITIONS[(ctx.state, event)]
        ctx.state = next_state
    return ctx.outbound
```

## 涓?step9 鐨勬灦鏋勫姣?

```
step9 (_agent_loop inline):       step10 (AgentLoop):
鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€     鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
鍑芥暟锛屼笉鍙鐢?                   绫伙紝鍙嫭绔嬪鍏?娴嬭瘯
鎵佸钩椤哄簭閫昏緫                      6鎬佺姸鎬佹満
鏃?session 閿?                    Per-session asyncio.Lock
鍗曠嚎绋嬩覆琛?                        璺?session 骞跺彂
寮傚父鐩存帴宕╂帀                      寮傚父琚崟鑾?鈫?error outbound
鍛戒护閫氳繃 metadata hack             鍛戒护鍦?CLI 灞傚鐞?
```

## 娴嬭瘯

14 涓柊娴嬭瘯锛?4 涓€昏锛夛細

| 娴嬭瘯 | 鍐呭 |
|------|------|
| `test_state_restore` | RESTORE 鍒涘缓/鍔犺浇 Session |
| `test_state_compact_noop` | COMPACT 鏃犲帇缂╂椂 noop |
| `test_state_compact_with_summary` | COMPACT 鐢熸垚鎽樿 |
| `test_state_build` | BUILD 缁勮 history + messages |
| `test_state_run` | RUN 杩愯 AgentRunner |
| `test_state_save` | SAVE 鎸佷箙鍖栨秷鎭?|
| `test_state_respond` | RESPOND 鍙戝竷 OutboundMessage |
| `test_state_transitions` | 楠岃瘉瀹屾暣杞崲閾?|
| `test_error_in_state_caught_by_process_message` | 寮傚父琚崟鑾蜂负 error outbound |
| `test_full_turn` | 瀹屾暣涓€杞?RESTORE鈫?..鈫扗ONE |
| `test_full_turn_with_history` | 澶氳疆娑堟伅鍘嗗彶鎸佷箙鍖?|
| `test_per_session_lock` | 鍚?session 涓茶 |
| `test_cross_session_concurrent` | 涓嶅悓 session 骞惰 |
| `test_loop_stop_exits` | stop() 閫€鍑哄惊鐜?|
| `test_agent_roundtrip_via_loop` | 鎬荤嚎寰€杩斿畬鏁存祴璇?|

## 鍏抽敭璁捐鍐崇瓥

| 鍐崇瓥 | 閫夋嫨 | 鐞嗙敱 |
|------|------|------|
| 鐘舵€佹暟 | 6 鎬侊紙鏃犻噺 COMMAND 鎬侊級 | 淇濇寔鏈€灏忓閲忥紝瀵归綈 roadmap |
| 鍛戒护澶勭悊浣嶇疆 | CLI 灞?| 鐘舵€佹満淇濇寔绾补锛屼笉娣峰叆鍛戒护璺敱 |
| 閿欒澶勭悊 | try/except 鈫?error outbound | 绠€鍗曟仮澶嶄笉宕?|
| Session 閿?| `asyncio.Lock()` per key | 闃叉鍚屼竴 session 骞跺彂鍐欏叆 |
| 璺?session 骞跺彂 | 鏃犱笂闄?| 鍚庣画鍙姞 Semaphore |

## 涓?nanobot 瀵归綈

```
nanobot/agent/loop.py 鈫?step10/loop.py (60% 瀵归綈)
  鐩稿悓: 鐘舵€佹灇涓?+ 杞崲琛?+ per-session lock + _dispatch + stop()
  绠€鍖? 鏃?COMMAND 鎬併€佹棤 Semaphore銆佹棤 streaming銆佹棤 pending_queue銆佹棤 runtime events
  鏈潵: step13 mid-turn injection, step12 streaming, step11 hooks
```

## 涓嬩竴绔?

Step 11 鈥?Hook 绯荤粺锛欰gentRun 鐢熷懡鍛ㄦ湡閽╁瓙锛坆efore_run, after_run, on_error, on_stream锛夈€?
