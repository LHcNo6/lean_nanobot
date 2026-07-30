from __future__ import annotations

import json
from typing import Any

_TRUNCATED_SUFFIX = "... (truncated)"


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
