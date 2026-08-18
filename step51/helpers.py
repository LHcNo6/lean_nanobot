from __future__ import annotations

import json
import re
from typing import Any

_TRUNCATED_SUFFIX = "... (truncated)"

_THINKING_TAGS = ("think", "thinking", "thought")


def _tag_regex(tags: tuple[str, ...]) -> str:
    return rf"(?:{'|'.join(re.escape(tag) for tag in tags)})"


_THINKING_TAG = _tag_regex(_THINKING_TAGS)
_THINKING_TAG_PREFIX = "|".join(
    sorted(
        {re.escape(tag[:i]) for tag in _THINKING_TAGS for i in range(1, len(tag) + 1)},
        key=len,
        reverse=True,
    )
)
_PARTIAL_THINKING_TAG = rf"</?(?:{_THINKING_TAG_PREFIX})>?"


def strip_think(text: str) -> str:
    """移除内联思考块与未闭合/畸形标签，返回净化后的文本。

    覆盖（对齐 nanobot ``utils/helpers.py:strip_think`` 最小集）：
    1. 成对的 ``<think>...</think>`` / ``<thinking>...`` / ``<thought>...``；
    2. 流式前缀里从未闭合的块（``<think>...`` 直达文末）；
    3. 缺 ``>`` 的畸形开标签（如 ``<think广场…``）；
    4. 孤立闭合标签仅在文首/文末剥离（避免误删正文讨论）；
    5. 流式分块截断在控制标签中间时，仅剥已知控制标签前缀。
    """
    text = re.sub(rf"<(?P<tag>{_THINKING_TAG})>[\s\S]*?</(?P=tag)>", "", text)
    text = re.sub(rf"^\s*<{_THINKING_TAG}>[\s\S]*$", "", text)
    text = re.sub(
        rf"<{_THINKING_TAG}(?![A-Za-z0-9_\-:>/])", "", text
    )
    text = re.sub(rf"^\s*</{_THINKING_TAG}>\s*", "", text)
    text = re.sub(rf"\s*</{_THINKING_TAG}>\s*$", "", text)
    text = re.sub(rf"{_PARTIAL_THINKING_TAG}$", "", text)
    return text.strip()


def strip_reasoning_tags(text: object) -> str:
    """移除已确认为 reasoning 内容的包裹标签。

    与 :func:`strip_think` 不同，这里假设整段文本都是推理内容，
    只剥首尾的 ``<think>`` / ``</think>`` 包装。
    """
    if not isinstance(text, str):
        return ""
    text = re.sub(rf"^\s*<{_THINKING_TAG}>\s*", "", text)
    text = re.sub(rf"\s*</{_THINKING_TAG}>\s*$", "", text)
    text = re.sub(rf"\s*(?:{_PARTIAL_THINKING_TAG})$", "", text)
    return text.strip()


def extract_think(text: str) -> tuple[str | None, str]:
    """从内联 ``<think>`` 标签提取思考内容。

    Args:
        text: 原始文本。

    Returns:
        ``(thinking_text, cleaned_text)``：只提取闭合块；未闭合的流式前缀
        从净化文本中剥掉但不作为思考内容返回（交给 :func:`strip_think`）。
    """
    parts: list[str] = []
    for m in re.finditer(rf"<(?P<tag>{_THINKING_TAG})>([\s\S]*?)</(?P=tag)>", text):
        parts.append(m.group(2).strip())
    thinking = "\n\n".join(parts) if parts else None
    return thinking, strip_think(text)


class IncrementalThinkExtractor:
    """流式缓冲的内联 ``<think>`` 增量提取器（对齐 nanobot 同名类）。

    流式 provider 只暴露单一内容 delta 通道；当模型把推理嵌在
    ``<think>...</think>`` 里时，需要把推理增量地浮现出来而不重发已发出的
    文本。本类持有"已发出"游标，runner 与 progress hook 共享同一形态。
    """

    __slots__ = ("_emitted",)

    def __init__(self) -> None:
        self._emitted = ""

    def reset(self) -> None:
        self._emitted = ""

    async def feed(self, buf: str, emit: Any) -> bool:
        """把 *buf* 中新出现的思考文本交给 ``emit``（async 单字符串回调）。

        Args:
            buf: 累计的流式缓冲。
            emit: async 回调（通常是 ``hook.emit_reasoning``）。

        Returns:
            True 表示本次调用发出了新推理片段。
        """
        thinking, _ = extract_think(buf)
        if not thinking or thinking == self._emitted:
            return False
        new = thinking[len(self._emitted):].strip()
        self._emitted = thinking
        if not new:
            return False
        await emit(new)
        return True


def extract_reasoning(
    reasoning_content: str | None,
    thinking_blocks: list[dict[str, Any]] | None,
    content: str | None,
) -> tuple[str | None, str | None]:
    """从一次模型响应提取 ``(reasoning_text, cleaned_content)``。

    单一事实来源，决定"这条响应带了多少推理、剥掉后剩什么答案文本"。
    优先级（对齐 nanobot ``utils/helpers.py:extract_reasoning``）：
    1. 专用 ``reasoning_content``（DeepSeek-R1 / Kimi / MiMo / OpenAI
       reasoning 模型）；
    2. Anthropic ``thinking_blocks``；
    3. 内联 ``<think>`` / ``<thought>`` 块。
    每个响应只贡献一个来源；高优先级存在时忽略低优先级，但内联
    ``<think>`` 标签始终从 content 中剥离，防止漏进最终答案。
    """
    if reasoning_content:
        return (
            strip_reasoning_tags(reasoning_content),
            strip_think(content) if content else content,
        )
    if thinking_blocks:
        parts = [
            strip_reasoning_tags(tb.get("thinking", ""))
            for tb in thinking_blocks
            if isinstance(tb, dict) and tb.get("type") == "thinking"
        ]
        joined = "\n\n".join(p for p in parts if p)
        return (joined or None), strip_think(content) if content else content
    if content:
        return extract_think(content)
    return None, content


def build_assistant_message(
    content: str | None,
    tool_calls: list[dict[str, Any]] | None = None,
    reasoning_content: str | None = None,
    thinking_blocks: list[dict] | None = None,
) -> dict[str, Any]:
    """构建 provider 安全的 assistant 消息（可选 reasoning 字段）。

    Args:
        content: 净化后的答案文本（可为空串）。
        tool_calls: OpenAI 风格工具调用列表。
        reasoning_content: 推理文本（写入消息供历史回放）。
        thinking_blocks: Anthropic 风格思考块。

    Returns:
        assistant 消息字典；reasoning 字段只在非空时写入。
    """
    msg: dict[str, Any] = {"role": "assistant", "content": content or ""}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    if reasoning_content is not None or thinking_blocks:
        msg["reasoning_content"] = (
            strip_reasoning_tags(reasoning_content)
            if reasoning_content is not None
            else ""
        )
    if thinking_blocks:
        msg["thinking_blocks"] = thinking_blocks
    return msg


def truncate_text(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[:max_chars] + _TRUNCATED_SUFFIX


def stringify_text_blocks(content: list[dict[str, Any]]) -> str | None:
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            return None
        if block.get("type") != "text":
            return None
        text = block.get("text")
        if not isinstance(text, str):
            return None
        parts.append(text)
    return "\n".join(parts)


def _empty_tool_result_message(tool_name: str) -> str:
    return f"({tool_name} completed with no output)"


def ensure_nonempty_tool_result(tool_name: str, content: Any) -> Any:
    if content is None:
        return _empty_tool_result_message(tool_name)
    if isinstance(content, str) and not content.strip():
        return _empty_tool_result_message(tool_name)
    if isinstance(content, list):
        if not content:
            return _empty_tool_result_message(tool_name)
        text_payload = stringify_text_blocks(content)
        if text_payload is not None and not text_payload.strip():
            return _empty_tool_result_message(tool_name)
    return content


def find_legal_message_start(messages: list[dict[str, Any]]) -> int:
    declared: set[str] = set()
    start = 0
    for i, msg in enumerate(messages):
        role = msg.get("role")
        if role == "assistant":
            for tc in msg.get("tool_calls") or []:
                if isinstance(tc, dict) and tc.get("id"):
                    declared.add(str(tc["id"]))
        elif role == "tool":
            tid = msg.get("tool_call_id")
            if tid and str(tid) not in declared:
                start = i + 1
                declared.clear()
    return start


def recent_message_start_index(
    messages: list[dict[str, Any]],
    max_messages: int,
    *,
    extend_to_user: bool = False,
) -> int:
    """返回最近回放窗口的起始索引。

    对齐 nanobot ``utils/helpers.py:recent_message_start_index``。

    逻辑：
    1. ``max_messages <= 0`` → 返回 ``len(messages)``（空窗口）。
    2. 不 ``extend_to_user`` 或消息数 <= max_messages → 返回尾部切片起点。
    3. ``extend_to_user`` 且窗口内已有 user → 返回尾部切片起点。
    4. ``extend_to_user`` 且窗口内无 user → 向前找最近的 user；
       若该 user 前一个是 ``_channel_delivery``，则包含前一个。
    5. 找不到 user → 返回尾部切片起点。

    Args:
        messages: 消息列表。
        max_messages: 最大消息数。
        extend_to_user: 是否向前扩展到最近的 user turn。

    Returns:
        起始索引（0 <= idx <= len(messages)）。
    """
    if max_messages <= 0:
        return len(messages)
    start_idx = max(0, len(messages) - max_messages)
    if not extend_to_user or len(messages) <= max_messages:
        return start_idx
    if any(messages[i].get("role") == "user" for i in range(start_idx, len(messages))):
        return start_idx
    recovered_user = next(
        (i for i in range(start_idx - 1, -1, -1) if messages[i].get("role") == "user"),
        None,
    )
    if recovered_user is None:
        return start_idx
    if recovered_user > 0 and messages[recovered_user - 1].get("_channel_delivery"):
        return recovered_user - 1
    return recovered_user


def estimate_message_tokens(message: dict[str, Any]) -> int:
    content = message.get("content")
    parts: list[str] = []
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                text = part.get("text", "")
                if text:
                    parts.append(text)
            else:
                parts.append(json.dumps(part, ensure_ascii=False))
    elif content is not None:
        parts.append(json.dumps(content, ensure_ascii=False))

    for key in ("name", "tool_call_id"):
        value = message.get(key)
        if isinstance(value, str) and value:
            parts.append(value)
    if message.get("tool_calls"):
        parts.append(json.dumps(message["tool_calls"], ensure_ascii=False))

    payload = "\n".join(parts)
    if not payload:
        return 4
    return max(4, len(payload) // 4 + 4)


def _estimate_tools_tokens(tools: list[dict[str, Any]]) -> int:
    if not tools:
        return 0
    text = json.dumps(tools, ensure_ascii=False)
    return len(text) // 4 + len(tools) * 2


def estimate_prompt_tokens(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
) -> int:
    msg_tokens = sum(estimate_message_tokens(m) for m in messages)
    per_msg_overhead = len(messages) * 4
    tool_tokens = _estimate_tools_tokens(tools) if tools else 0
    return msg_tokens + tool_tokens + per_msg_overhead


def estimate_prompt_tokens_chain(
    provider: Any,
    model: str | None,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
) -> tuple[int, str]:
    provider_counter = getattr(provider, "estimate_prompt_tokens", None)
    if callable(provider_counter):
        try:
            tokens, source = provider_counter(messages, tools, model)
            if isinstance(tokens, (int, float)) and tokens > 0:
                return int(tokens), str(source or "provider_counter")
        except Exception:
            pass
    estimated = estimate_prompt_tokens(messages, tools)
    if estimated > 0:
        return int(estimated), "char_estimate"
    return 0, "none"


# ---- step51：SSRF/workspace 安全检测辅助函数 ----

_MAX_REPEAT_EXTERNAL_LOOKUPS = 2
_MAX_REPEAT_WORKSPACE_VIOLATIONS = 2

_OUTSIDE_PATH_PATTERN = re.compile(
    r"(?:^|[\s|>'\"])((?:/[^\s\"'>;|<]+)|(?:~[^\s\"'>;|<]+))"
)


def external_lookup_signature(tool_name: str, arguments: Any) -> str | None:
    """为需要限流的重复外部查找生成稳定签名。"""
    if not isinstance(arguments, dict):
        return None
    if tool_name == "web_fetch":
        url = str(arguments.get("url") or "").strip()
        if url:
            return f"web_fetch:{url.lower()}"
    if tool_name == "web_search":
        query = str(arguments.get("query") or arguments.get("search_term") or "").strip()
        if query:
            return f"web_search:{query.lower()}"
    return None


def repeated_external_lookup_error(
    tool_name: str,
    arguments: Any,
    seen_counts: dict[str, int],
) -> str | None:
    """超过重试预算后阻断重复外部查找。"""
    signature = external_lookup_signature(tool_name, arguments)
    if signature is None:
        return None
    count = seen_counts.get(signature, 0) + 1
    seen_counts[signature] = count
    if count <= _MAX_REPEAT_EXTERNAL_LOOKUPS:
        return None
    return (
        "Error: repeated external lookup blocked. "
        "Use the results you already have to answer, or try a meaningfully different source."
    )


def _normalize_violation_target(raw: str) -> str:
    """规范化路径，使等价写法命中同一个 key。"""
    from pathlib import Path
    try:
        normalized = Path(raw).expanduser().resolve().as_posix()
    except Exception:
        normalized = raw.replace("\\", "/")
    return f"violation:{normalized}".lower()


def workspace_violation_signature(
    tool_name: str,
    arguments: Any,
) -> str | None:
    """为工作区外目标生成跨工具稳定签名。"""
    if not isinstance(arguments, dict):
        return None
    for key in ("path", "file_path", "target", "source", "destination"):
        val = arguments.get(key)
        if isinstance(val, str) and val.strip():
            return _normalize_violation_target(val.strip())
    if tool_name in {"exec", "shell"}:
        cmd = str(arguments.get("command") or "").strip()
        if cmd:
            match = _OUTSIDE_PATH_PATTERN.search(cmd)
            if match:
                return _normalize_violation_target(match.group(1))
        cwd = str(arguments.get("working_dir") or "").strip()
        if cwd:
            return _normalize_violation_target(cwd)
    return None


def repeated_workspace_violation_error(
    tool_name: str,
    arguments: Any,
    seen_counts: dict[str, int],
) -> str | None:
    """重复绕过尝试后返回升级错误。"""
    signature = workspace_violation_signature(tool_name, arguments)
    if signature is None:
        return None
    count = seen_counts.get(signature, 0) + 1
    seen_counts[signature] = count
    if count <= _MAX_REPEAT_WORKSPACE_VIOLATIONS:
        return None
    target = signature.split("violation:", 1)[1] if "violation:" in signature else signature
    return (
        "Error: refusing repeated workspace-bypass attempts.\n"
        f"You have tried to access '{target}' (or an equivalent path) "
        f"{count} times in this turn. This is a hard policy boundary -- "
        "switching tools, shell tricks, working_dir overrides, symlinks, "
        "or base64 piping will NOT change the answer. Stop retrying. "
        "If the user genuinely needs this resource, tell them you cannot "
        "access it and ask how they want to proceed."
    )
