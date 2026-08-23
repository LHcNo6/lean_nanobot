# step125 design：通道清洗（G6 通道部分）

## 1. 模块职责划分

| 模块 | 职责 |
| --- | --- |
| `utils/subagent_channel_display.py` | 纯函数：把完整 announce 文本清洗为人类友好的通道文本 |
| `command/builtin.py._cmd_history` | 展示边界：渲染会话历史时对 `subagent_result` 行应用清洗 |

## 2. `utils/subagent_channel_display.py` 设计

### 2.1 常量

```python
_SUBAGENT_CHANNEL_RESULT_MAX_CHARS = 800
```

与 nanobot 一致：限制 `Result:` 正文展示长度，保证通道/历史可读；全文仍在磁盘供 LLM replay。

### 2.2 `scrub_subagent_announce_body(content: str) -> str`

逐字对齐 `nanobot/utils/subagent_channel_display.py`：

1. 归一化换行 `\r\n` → `\n` 并 `strip()`。
2. 若首行以 `[Subagent` 开头，取首行作为 `header`。
3. 大小写不敏感定位 `\nresult:\n`（回退 `\nresult:`）得到 `after` 段。
4. 在 `after` 中删除尾随的 `summarize this naturally` 模型指令（含其后的所有内容）。
5. `body` 超过 800 字符时截断为 `body[:799].rstrip() + "…"`。
6. 返回 `f"{header}\n\n{body}"`（两者皆有时），否则返回 header / body / 原文其一。

> 关键：函数**不修改入参**，返回新字符串（announce 全文在持久化层不变）。

### 2.3 `scrub_subagent_messages_for_channel(messages: list[dict]) -> None`

遍历消息字典，对 `injected_event == "subagent_result"` 且 `content` 为字符串的消息，
原地将其 `content` 替换为 `scrub_subagent_announce_body(content)`。保留 nanobot 同名 API，
供将来更多展示边界（如会话预览 / WebSocket）复用。

## 3. `_cmd_history` 展示边界改造

现有逻辑（`command/builtin.py`）：

```python
for i, m in enumerate(session.messages):
    if is_hidden_history_message(m):
        continue
    role = m["role"].ljust(9)
    content = (m.get("content") or "")[:80]
    ...
```

改造点（最小增量：仅对 `subagent_result` 行做清洗）：

```python
from step125.utils.subagent_channel_display import scrub_subagent_announce_body

for i, m in enumerate(session.messages):
    if is_hidden_history_message(m):
        continue
    role = m["role"].ljust(9)
    content = m.get("content") or ""
    if m.get("injected_event") == "subagent_result":
        content = scrub_subagent_announce_body(content)
    content = content[:80]
    ...
```

- 仅当 `injected_event == "subagent_result"` 时清洗；其余消息仍按原 80 字符截断。
- 清洗在「渲染时」发生，不写回 `session.messages`，持久化全文不变（LLM replay 完整）。

## 4. 不变更的部分（刻意保持）

- `subagent.py._announce`：仍发布全文 announce（头 + Task + Result + Summarize）。
- `_persist_subagent_followup`：仍持久化全文 `subagent_result` assistant 消息。
- `get_history` / runner：模型上下文仍使用完整 announce（通道清洗只发生在展示边界）。

## 5. 风险与缓解

- **解析对模板格式敏感**：若未来 `subagent_announce.md` 改掉 `Result:` 关键字，清洗会回退到
  返回 header / 原文（函数已对缺失 Result 段做兜底），不会抛错或丢失信息。
- **展示截断 80 字符**：header 占约 40 字符，result 片段约 40 字符可见；足够证明脚手架被清除，
  且清洗核心是「去掉 Task:/Summarize」而非「展示更多」。
