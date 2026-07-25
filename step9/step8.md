# Step 8 鈥?鑷姩鍘嬬缉锛圱oken-aware Consolidation锛?

## 鐩爣

涓?step7 鐨?Session 鍔犱笂 **token 棰勭畻鎺у埗**锛氳秴鍑洪绠楁椂鑷姩鍘嬬缉鏃ф秷鎭€佺敤 LLM 鎬荤粨鎽樿銆佹敞鍏?system prompt銆?

## 鏂囦欢缁撴瀯

```
step8/
鈹溾攢鈹€ __init__.py
鈹溾攢鈹€ llm.py, provider.py, openai_compat_provider.py    # from step7
鈹溾攢鈹€ tool.py, tools/echo.py                             # from step7
鈹溾攢鈹€ runner.py                                           # from step7
鈹溾攢鈹€ session.py                  鈽?淇敼: get_history(max_tokens=...)
鈹溾攢鈹€ context.py                  鈽?淇敼: build_system_prompt(session_summary=...)
鈹溾攢鈹€ consolidation.py            鈽?NEW: TokenEstimator + Consolidator
鈹溾攢鈹€ main.py                     鈽?NEW: token-budget-aware CLI
鈹溾攢鈹€ test.py                     鈽?NEW: 21 涓祴璇?
鈹斺攢鈹€ step8.md
```

## TokenEstimator

```python
def estimate_message_tokens(msg: dict) -> int:
    # content (str / list[text blocks]) + name + tool_call_id + tool_calls
    # 鈮?len(payload) // 4 + 4
    return max(4, len(payload) // 4 + 4)

def estimate_prompt_tokens(messages: list[dict]) -> int:
    return sum(estimate_message_tokens(m) for m in messages) + 4 * len(messages)
```

瀛楃绾т及绠楋紝涓嶄緷璧?tiktoken銆?

## Consolidator

```python
@dataclass
class Consolidator:
    provider: LLMProvider | None = None    # None = 鏃犳憳瑕侊紙鍙埅鏂級
    consolidation_ratio: float = 0.5       # 鍘嬬缉鍚庝繚鐣?50% 鐨勯绠?

    async def maybe_consolidate(session, max_tokens, model=None) -> str | None
```

**娴佺▼**锛?
```
maybe_consolidate(session, max_tokens):
  鈹?
  鈹溾攢 1. unconsolidated = session.messages[last_consolidated:]
  鈹溾攢 2. estimated = estimate_prompt_tokens(unconsolidated)
  鈹溾攢 3. target = max_tokens 脳 consolidation_ratio
  鈹溾攢 4. 濡傛灉 estimated 鈮?target 鈫?return None锛堟棤闇€鍘嬬缉锛?
  鈹?
  鈹溾攢 5. _find_boundary(unconsolidated, target)
  鈹?    浠庡悗寰€鍓嶇疮鍔?token锛屾壘鍒拌兘鏀惧叆 target 鐨勪綅缃?
  鈹?    鍐嶅榻愬埌鏈€杩戠殑 user 杞
  鈹?
  鈹溾攢 6. 濡傛灉鏈?provider:
  鈹?    _archive(to_archive) 鈫?LLM 鎽樿 鈫?summary text
  鈹?
  鈹溾攢 7. session.last_consolidated += boundary
  鈹溾攢 8. 濡傛灉浜х敓鎽樿: session.metadata["_last_summary"] = {...}
  鈹斺攢 9. return summary
```

`_archive()` 浣跨敤纭紪鐮佺殑 system prompt 璁?LLM 鎬荤粨瀵硅瘽鐗囨鐨?key facts銆?

## `get_history(max_tokens)` 鏀归€?

```python
# Session.get_history() 鏂板 max_tokens 鍙傛暟
def get_history(self, max_messages=50, max_tokens=0):
    unconsolidated = self.messages[self.last_consolidated:]
    
    if max_tokens > 0:
        # 浠庡悗寰€鍓嶏紝鍦?token 棰勭畻鍐呬繚鐣欏熬閮?
        for msg in reversed(unconsolidated):
            ...
        unconsolidated = kept
    
    if max_messages > 0:
        unconsolidated = unconsolidated[-max_messages:]
    
    return list(unconsolidated)
```

娉ㄦ剰椤哄簭锛氬厛鎸?token 鍒囷紝鍐嶆寜鏉℃暟鍒囥€?

## `build_system_prompt(session_summary)` 鏀归€?

```python
def build_system_prompt(self, identity=None, session_summary=None):
    parts = [identity or _DEFAULT_IDENTITY]
    # bootstrap files...
    if session_summary:
        parts.append(f"[Archived Context Summary]\n\n{session_summary}")
    return "\n\n---\n\n".join(parts)
```

鎽樿鍑虹幇鍦?system prompt 鏈熬銆?

## 闆嗘垚娴佺▼

```python
# 1. 灏濊瘯鍘嬬缉
summary = await consolidator.maybe_consolidate(session, max_tokens=budget)

# 2. 鑾峰彇 token 棰勭畻鍐呯殑 history
history = session.get_history(max_messages=50, max_tokens=budget)

# 3. 鏋勫缓 context锛堝惈鎽樿锛?
msgs = context.build_messages(message, history=history, session_summary=summary)

# 4. 杩愯 AgentRunner
result = await AgentRunner().run(spec)

# 5. 淇濆瓨鏂版秷鎭?
session.import_messages(result.messages[1 + len(history):])
session_manager.save(session)
```

## 涓?nanobot 瀵规瘮

| 鐗规€?| nanobot | step8 |
|---|---|---|
| Token 浼扮畻 | tiktoken cl100k_base | 瀛楃绾?(len//4) |
| consolidation_ratio | 0.5锛堝彲閰嶇疆锛?| 0.5锛堢‖缂栫爜锛?|
| 杈圭晫瀵归綈 | `pick_consolidation_boundary()` + user 杞 | `_find_boundary()` + user 瀵归綈 |
| 鎽樿 prompt | `consolidator_archive.md` Jinja2 妯℃澘 | 纭紪鐮佸瓧绗︿覆 |
| 澶氳疆鍘嬬缉 | 寰幆 5 杞洿鍒?鈮?target | 鍗曡疆 |
| 鎽樿瀛樺偍 | `metadata["_last_summary"]` | 鍚?|
| 鎽樿娉ㄥ叆 | `build_system_prompt(session_summary=...)` | 鍚?|
| 鍘嬬缉瑙﹀彂鏃舵満 | BUILD 闃舵 + 鍚庡彴 | 姣忔 turn 鍓嶄富鍔ㄨ皟鐢?|
| history.jsonl | `MemoryStore.append_history()` | 鏆傛湭瀹炵幇锛坰tep15锛?|
| Dream 钂搁 | 鏈?| 鏃?|

## 娴嬭瘯瑕嗙洊锛?1 涓級

| # | 娴嬭瘯 | 鍦烘櫙 |
|---|------|------|
| 1鈥? | Token 浼扮畻 | 鏂囨湰/tool_calls/tool_result/prompt_tokens |
| 6鈥? | get_history(max_tokens) | 闄愬埗/鍏ㄩ儴/zero/last_consolidated |
| 10鈥?1 | _find_boundary | 鍏ㄩ儴淇濈暀/鎴柇閮ㄥ垎 |
| 12鈥?5 | maybe_consolidate | 涓嶈秴棰勭畻/鏃?provider/鏈?provider/鏃犳秷鎭?|
| 16鈥?7 | _format_messages | 鏅€?鍚?tool_calls |
| 18鈥?0 | session_summary | 鍦?system prompt/in build_messages/鏃犳憳瑕?|
| 21 | 瀹屾暣闆嗘垚 | Consolidator 鈫?get_history 鈫?build_messages |

## 鏆撮湶鐨勯棶棰?

1. **鍗曡疆鍘嬬缉** 鈥?濡傛灉鍓╀綑鍘嗗彶浠嶇劧瓒呴绠楋紝闇€瑕佸閮ㄥ惊鐜皟鐢?
2. **瀛楃绾?token 浼扮畻涓嶅噯** 鈥?涓枃鍜岃嫳鏂囨贩鐢ㄦ椂鍋忓樊杈冨ぇ锛堝悗缁彲鍔?tiktoken锛?
3. **鎽樿 prompt 纭紪鐮?* 鈥?涓嶆槸妯℃澘鏂囦欢锛岀敤鎴锋棤娉曡嚜瀹氫箟
4. **涓?step7 main.py 鐨勫吋瀹规€?* 鈥?`get_history` 绛惧悕鎵╁睍浜?`max_tokens`锛屼絾榛樿涓?0锛堝悜鍚庡吋瀹癸級
5. **main.py 鐨?`/new` 璁块棶绉佹湁灞炴€?* 鈥?鍚?step7

## 涓嬩竴姝?

**Step 9锛歁essageBus** 鈥?`asyncio.Queue` 椹卞姩 inbound/outbound 娑堟伅閫氶亾锛屼负澶氶€氶亾鍋氬噯澶囥€?
