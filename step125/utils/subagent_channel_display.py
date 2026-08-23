"""通道清洗：把子代理 announce 的内部脚手架剥离为人类友好的通道文本。

对齐 ``nanobot/utils/subagent_channel_display.py``。持久化的子代理 announce 镜像
``agent/subagent_announce.md``：头、`Task:` 任务上下文（供模型理解）、`Result:` 结果，
以及尾部的模型专用 `Summarize this naturally` 指令。外部通道（/history 历史展示、会话预览）
只应展示「头 + 截断的结果正文」，内部脚手架不在人类可见表面出现。
"""

from __future__ import annotations

from typing import Any

# 限制 Result 段展示长度，保证通道/历史可读；全文仍在磁盘供 LLM replay
# （仅在对外的展示副本上做截断）。
_SUBAGENT_CHANNEL_RESULT_MAX_CHARS = 800


def scrub_subagent_announce_body(content: str) -> str:
    """从完整子代理 announce 文本中派生出通道安全文本。

    保留 ``[Subagent ...]`` 头与截断后的 ``Result:`` 正文，移除 ``Task:`` 任务上下文
    与尾部的 ``Summarize this naturally`` 模型指令。不修改入参，返回新字符串。

    Args:
        content: 完整 announce 文本（含头、Task、Result、Summarize 脚手架）。

    Returns:
        清洗后的文本：头 + 换行 + 截断结果正文；缺失 Result 段时回退为头或原文。
    """
    stripped = content.replace("\r\n", "\n").strip()
    lines = stripped.splitlines()
    header = ""
    if lines and lines[0].startswith("[Subagent"):
        header = lines[0].strip()

    lower = stripped.lower()
    key = "\nresult:\n"
    ri = lower.find(key)
    if ri == -1:
        key = "\nresult:"
        ri = lower.find(key)
    if ri == -1:
        return header if header else stripped

    after = stripped[ri + len(key):].lstrip()
    summ_marker = "summarize this naturally"
    si = after.lower().find(summ_marker)
    if si != -1:
        after = after[:si].rstrip()

    body = after.strip()
    limit = _SUBAGENT_CHANNEL_RESULT_MAX_CHARS
    if limit and len(body) > limit:
        body = body[: limit - 1].rstrip() + "…"
    if header and body:
        return f"{header}\n\n{body}"
    return header or body or stripped


def scrub_subagent_messages_for_channel(messages: list[dict[str, Any]]) -> None:
    """原地清洗携带 ``subagent_result`` 注入标记的消息正文。

    遍历消息字典，对 ``injected_event == "subagent_result"`` 且 ``content`` 为字符串的消息，
    把其 ``content`` 替换为 ``scrub_subagent_announce_body(content)``。供多个展示边界复用。

    Args:
        messages: 消息字典列表（同 ``session.messages`` 形态）。原地修改。
    """
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("injected_event") != "subagent_result":
            continue
        raw = msg.get("content")
        if not isinstance(raw, str) or not raw.strip():
            continue
        msg["content"] = scrub_subagent_announce_body(raw)
