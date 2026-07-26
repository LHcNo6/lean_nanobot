# Step 9 鈥?寮傛娑堟伅鎬荤嚎锛圡essageBus锛?

## 鐩爣

灏嗘秷鎭殑鐢熶骇鑰咃紙CLI 杈撳叆锛変笌娑堣垂鑰咃紙Agent 澶勭悊绠￠亾锛夐€氳繃 **寮傛娑堟伅鎬荤嚎** 瑙ｈ€︼紝涓哄悗缁楠ゅ紩鍏ュ閫氶亾銆丄gentLoop 鐘舵€佹満鍋氬噯澶囥€?

## 鏂板鏂囦欢

| 鏂囦欢 | 琛屾暟 | 鑱岃矗 |
|------|------|------|
| `events.py` | 22 | `InboundMessage` / `OutboundMessage` 鏁版嵁绫?|
| `bus.py` | 26 | `MessageBus` 鈥?涓や釜 `asyncio.Queue`锛坕nbound/outbound锛?|

## 淇敼鏂囦欢

| 鏂囦欢 | 鍙樺寲 |
|------|------|
| `main.py` | 瀹屽叏閲嶅啓涓?**鎬荤嚎椹卞姩** 鏋舵瀯 |
| `test.py` | 澧炲姞 9 涓€荤嚎娴嬭瘯锛屽叡 30 涓祴璇?|

## 璁捐

### 鏁版嵁娴佸姣?

**Before (step8)锛?*
```
main() 寰幆:
  杈撳叆 鈫?SessionManager 鈫?Consolidator 鈫?ContextBuilder 鈫?AgentRunner 鈫?杈撳嚭
  鍏ㄩ儴涓茶锛屼竴涓嚎绋?
```

**After (step9)锛?*
```
鈹屸攢 main() 鍓嶅彴 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹?
鈹? 杈撳叆 鈫?bus.publish_inbound()      鈹?
鈹? bus.consume_outbound() 鈫?杈撳嚭     鈹?
鈹斺攢鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹攢鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹攢鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹?
           鈹?inbound      鈹?outbound
           鈻?             鈻?
    鈹屸攢鈹€鈹€鈹€ MessageBus 鈹€鈹€鈹€鈹€鈹€鈹€鈹?
    鈹? inbound: Queue       鈹?
    鈹? outbound: Queue      鈹?
    鈹斺攢鈹€鈹€鈹€鈹攢鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹攢鈹€鈹€鈹?
         鈹?consume      鈹?publish
         鈻?             鈹?
鈹屸攢 _agent_loop (bg) 鈹€鈹€鈹€鈹€鈹樷攢鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹?
鈹? SessionManager 鈫?Consolidator    鈹?
鈹? ContextBuilder 鈫?AgentRunner     鈹?
鈹? bus.publish_outbound()           鈹?
鈹斺攢鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹?
```

### 浜嬩欢鏁版嵁绫?

```python
@dataclass
class InboundMessage:
    content: str
    channel: str = "cli"            # 鏉ユ簮閫氶亾
    sender_id: str = ""             # 鐢ㄦ埛鏍囪瘑
    chat_id: str = "default"        # 浼氳瘽鏍囪瘑
    timestamp: datetime = ...       # 鑷姩鐢熸垚
    session_key: str | None = None  # session 瑕嗙洊
    metadata: dict = {}             # 鍛戒护/鎺у埗淇℃伅

@dataclass
class OutboundMessage:
    content: str
    channel: str = "cli"
    chat_id: str = "default"
    metadata: dict = {}             # stop_reason/tokens 绛変俊鎭?
```

### MessageBus API

| 鏂规硶 | 璇存槑 |
|------|------|
| `publish_inbound(msg)` | 鍚?inbound 闃熷垪鎶曢€掓秷鎭?|
| `consume_inbound()` | 闃诲鑾峰彇涓嬩竴鏉″叆绔欐秷鎭?|
| `publish_outbound(msg)` | 鍚?outbound 闃熷垪鎶曢€掑搷搴?|
| `consume_outbound()` | 闃诲鑾峰彇涓嬩竴鏉″嚭绔欐秷鎭?|
| `inbound_size` / `outbound_size` | 闃熷垪褰撳墠闀垮害锛堝睘鎬э級 |

## 鏍稿績鏀瑰姩锛歮ain.py

### 浠ｇ悊浠诲姟锛坃agent_loop锛?

鍚庡彴 `asyncio.Task`锛屽惊鐜皟鐢?`bus.consume_inbound()`锛屽姣忔潯娑堟伅锛?

1. 瑙ｆ瀽 metadata 涓殑 `command`锛坄/exit`銆乣/history`銆乣/new`锛?
2. `SessionManager.get_or_create()` 鑾峰彇/鍒涘缓浼氳瘽
3. `Consolidator.maybe_consolidate()` 妫€鏌ユ槸鍚﹂渶瑕佸帇缂?
4. `Session.get_history(max_tokens=budget)` 鑾峰彇 token 棰勭畻鍐呯殑鍘嗗彶
5. `ContextBuilder.build_messages()` 缁勮 system + history + user
6. `AgentRunner.run()` 鎵ц LLM 璋冪敤 + 宸ュ叿
7. `Session.import_messages()` + `SessionManager.save()` 鎸佷箙鍖?
8. `bus.publish_outbound()` 杩斿洖缁撴灉

### 鍓嶅彴涓诲惊鐜?

```python
bus = MessageBus()
agent = asyncio.create_task(_agent_loop(bus, ...))

while True:
    text = await ainput("You: ")
    await bus.publish_inbound(InboundMessage(content=text))
    resp = await bus.consume_outbound()
    print(resp.content)
```

鍛戒护閫氳繃 `metadata["command"]` 浼犻€掞紝浠ｇ悊浠诲姟鍝嶅簲鍚庤繑鍥炲甫瀵瑰簲 metadata 鐨?outbound 娑堟伅銆?

## 娴嬭瘯

9 涓柊澧炴祴璇曪細

| 娴嬭瘯 | 鍐呭 |
|------|------|
| `test_publish_consume_inbound` | 鍙戝竷骞舵秷璐?InboundMessage |
| `test_publish_consume_outbound` | 鍙戝竷骞舵秷璐?OutboundMessage |
| `test_multiple_messages_fifo` | 5 鏉℃秷鎭?FIFO 椤哄簭 |
| `test_inbound_size` | inbound 闃熷垪闀垮害璺熻釜 |
| `test_outbound_size` | outbound 闃熷垪闀垮害璺熻釜 |
| `test_inbound_message_fields` | InboundMessage 鍚勫瓧娈?|
| `test_outbound_message_fields` | OutboundMessage 鍚勫瓧娈?|
| `test_concurrent_producer_consumer` | 100 鏉℃秷鎭苟鍙戞棤涓㈠け |
| `test_agent_roundtrip` | 瀹屾暣鎬荤嚎寰€杩旓紙inbound 鈫?澶勭悊 鈫?outbound锛?|

## 鍏抽敭鍐崇瓥

| 鍐崇瓥 | 閫夋嫨 | 鍘熷洜 |
|------|------|------|
| Queue 鏄惁 bounded | 鍚︼紙unbounded锛?| 瀵归綈 nanobot锛屽綋鍓嶆棤鍙嶅帇闇€姹?|
| 鍛戒护澶勭悊鏂瑰紡 | metadata["command"] | 涓嶉渶瑕侀澶栨満鍒讹紝浠ｇ悊浠诲姟妫€鏌ュ嵆鍙?|
| Session/chat 璺敱 | session_key 瑕嗙洊鎴?chat_id | 鎬荤嚎灞傛劅鐭ヤ細璇濇蹇?|
| agent 鍙栨秷绛栫暐 | main 閫€鍑烘椂 `agent.cancel()` | 骞插噣鍏抽棴 |

## 涓嬩竴绔?

Step 10 鈥?AgentLoop 鐘舵€佹満锛氬皢 `_agent_loop` 閲嶆瀯涓烘寮忕殑鐘舵€佹満锛圧ESTORE 鈫?BUILD 鈫?RUN 鈫?SAVE 鈫?RESPOND 鈫?DONE锛夈€?
