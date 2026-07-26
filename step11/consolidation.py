from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from step11.session import Session


def estimate_message_tokens(msg: dict[str, Any]) -> int:
    parts: list[str] = []
    content = msg.get("content", "")
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
    if "name" in msg and isinstance(msg["name"], str):
        parts.append(msg["name"])
    if "tool_call_id" in msg and isinstance(msg["tool_call_id"], str):
        parts.append(msg["tool_call_id"])
    if "tool_calls" in msg:
        parts.append(json.dumps(msg["tool_calls"], ensure_ascii=False))
    payload = "\n".join(parts)
    return max(4, len(payload) // 4 + 4)


def estimate_prompt_tokens(messages: list[dict[str, Any]]) -> int:
    return sum(estimate_message_tokens(m) for m in messages) + 4 * len(messages)


_CONSOLIDATOR_SYSTEM_PROMPT = (
    "You are a conversation archivist. Summarize the following conversation segment.\n"
    "Extract key facts, decisions, user preferences, and important context.\n"
    "The summary will be injected into future system prompts.\n"
    "Be concise but thorough."
)


@dataclass
class Consolidator:
    provider: Any | None = None
    consolidation_ratio: float = 0.5

    async def maybe_consolidate(
        self,
        session: Session,
        max_tokens: int,
        model: str | None = None,
    ) -> str | None:
        unconsolidated = session.messages[session.last_consolidated:]
        if not unconsolidated:
            return None

        estimated = estimate_prompt_tokens(unconsolidated)
        target = int(max_tokens * self.consolidation_ratio)
        if estimated <= target:
            return None

        boundary = self._find_boundary(unconsolidated, target)
        if boundary <= 0:
            return None

        to_archive = unconsolidated[:boundary]
        summary: str | None = None

        if self.provider is not None and to_archive:
            summary = await self._archive(to_archive, model=model)

        session.last_consolidated += boundary
        if summary:
            session.metadata["_last_summary"] = {
                "text": summary,
                "timestamp": datetime.now().isoformat(),
            }

        return summary

    @staticmethod
    def _find_boundary(
        unconsolidated: list[dict[str, Any]], target_tokens: int
    ) -> int:
        kept_tokens = 0
        keep_count = 0
        for msg in reversed(unconsolidated):
            tokens = estimate_message_tokens(msg)
            if keep_count > 0 and kept_tokens + tokens > target_tokens:
                break
            kept_tokens += tokens
            keep_count += 1
        boundary = len(unconsolidated) - keep_count
        while boundary < len(unconsolidated):
            if unconsolidated[boundary].get("role") == "user":
                break
            boundary += 1
        return boundary

    async def _archive(
        self, messages: list[dict[str, Any]], model: str | None = None
    ) -> str | None:
        formatted = self._format_messages(messages)
        try:
            response = await self.provider.chat_with_retry(
                messages=[
                    {"role": "system", "content": _CONSOLIDATOR_SYSTEM_PROMPT},
                    {"role": "user", "content": formatted},
                ],
                model=model,
                max_tokens=512,
            )
            return response.content
        except Exception:
            return None

    @staticmethod
    def _format_messages(messages: list[dict[str, Any]]) -> str:
        lines: list[str] = []
        for m in messages:
            role = m.get("role", "unknown")
            content = m.get("content", "") or ""
            tc = m.get("tool_calls")
            name = m.get("name", "")
            extra = ""
            if tc:
                extra += f"\n[tool_calls: {json.dumps(tc, ensure_ascii=False)}]"
            if name:
                extra += f"\n[tool_result for tool: {name}]"
            lines.append(f"[{role}]\n{content}{extra}")
        return "\n\n---\n\n".join(lines)
