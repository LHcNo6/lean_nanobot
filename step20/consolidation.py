from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any

from step20.helpers import (
    estimate_message_tokens,
    find_legal_message_start,
    truncate_text,
)
from step20.llm import Runtime
from step20.memory import MemoryStore, _ARCHIVE_SUMMARY_MAX_CHARS, _RAW_ARCHIVE_MAX_CHARS
from step20.session import Session, SessionManager

_SAFETY_BUFFER = 1024
_MAX_CONSOLIDATION_ROUNDS = 5
_CONSOLIDATOR_SYSTEM_PROMPT = (
    "You are a conversation archivist. Summarize the following conversation segment. "
    "Extract key facts, decisions, user preferences, and important context. "
    "The summary will be injected into future system prompts. Be concise but thorough."
)


def _consolidation_boundary(unconsolidated: list[dict[str, Any]], target_tokens: int) -> int:
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


class Consolidator:
    def __init__(
        self,
        store: MemoryStore,
        sessions: SessionManager,
        build_messages: Any,
        get_tool_definitions: Any,
        consolidation_ratio: float = 0.5,
        provider: Any = None,
    ) -> None:
        self.store = store
        self.sessions = sessions
        self.consolidation_ratio = consolidation_ratio
        self._build_messages = build_messages
        self._get_tool_definitions = get_tool_definitions
        self._locks: dict[str, asyncio.Lock] = {}
        self.provider = provider

    def get_lock(self, session_key: str) -> asyncio.Lock:
        if session_key not in self._locks:
            self._locks[session_key] = asyncio.Lock()
        return self._locks[session_key]

    # -- public API: backward-compatible --

    async def maybe_consolidate(self, session: Session, max_tokens: int, model: str | None = None) -> str | None:
        unconsolidated = session.messages[session.last_consolidated:]
        if not unconsolidated:
            return None
        from step20.helpers import estimate_prompt_tokens
        estimated = estimate_prompt_tokens(unconsolidated)
        target = int(max_tokens * self.consolidation_ratio)
        if estimated <= target:
            return None
        boundary = _consolidation_boundary(unconsolidated, target)
        if boundary <= 0:
            return None
        to_archive = unconsolidated[:boundary]
        summary = None
        if self.provider is not None and to_archive:
            summary = await self._archive_llm(to_archive, model=model)
        session.last_consolidated += boundary
        if summary:
            session.metadata["_last_summary"] = {
                "text": summary,
                "timestamp": datetime.now().isoformat(),
            }
        return summary

    async def _archive_llm(self, messages: list[dict[str, Any]], model: str | None = None) -> str | None:
        formatted = self._format_messages(messages)
        prov = getattr(self, "provider", None)
        if prov is not None:
            try:
                resp = await prov.chat(messages=[{"role": "user", "content": formatted}], model=model)
                if resp and resp.content:
                    return resp.content
            except Exception:
                pass
        return None

    def _format_messages(self, messages: list[dict[str, Any]]) -> str:
        return MemoryStore._format_messages(messages)

    # -- new public API: token-budget-driven --

    def pick_consolidation_boundary(self, session: Session, tokens_to_remove: int) -> tuple[int, int] | None:
        start = session.last_consolidated
        if start >= len(session.messages) or tokens_to_remove <= 0:
            return None
        removed_tokens = 0
        last_boundary = None
        for idx in range(start, len(session.messages)):
            message = session.messages[idx]
            if idx > start and message.get("role") == "user":
                last_boundary = (idx, removed_tokens)
                if removed_tokens >= tokens_to_remove:
                    return last_boundary
            removed_tokens += estimate_message_tokens(message)
        return last_boundary

    def _full_unconsolidated_history(self, session: Session) -> list[dict[str, Any]]:
        count = len(session.messages) - session.last_consolidated
        if count <= 0:
            return []
        return session.get_history(max_messages=count)

    def _input_token_budget(self, runtime: Runtime) -> int:
        return runtime.context_window_tokens - runtime.max_tokens - _SAFETY_BUFFER

    def _truncate_to_token_budget(self, text: str, runtime: Runtime) -> str:
        budget = self._input_token_budget(runtime)
        if budget <= 0:
            return truncate_text(text, _RAW_ARCHIVE_MAX_CHARS)
        chars = budget * 4
        return truncate_text(text, chars)

    def _persist_last_summary(self, session: Session, summary: str | None) -> None:
        if summary and summary != "(nothing)":
            session.metadata["_last_summary"] = {
                "text": summary,
                "last_active": session.updated_at,
            }

    async def archive(self, messages: list[dict[str, Any]], *, runtime: Runtime, session_key: str | None = None, summary_messages: list[dict[str, Any]] | None = None) -> str | None:
        if not messages:
            return None
        msgs = summary_messages if summary_messages is not None else messages
        formatted = self._format_messages(msgs)
        formatted = self._truncate_to_token_budget(formatted, runtime)
        try:
            response = await runtime.provider.chat(
                messages=[
                    {"role": "system", "content": _CONSOLIDATOR_SYSTEM_PROMPT},
                    {"role": "user", "content": formatted},
                ],
                model=runtime.model,
                max_tokens=1024,
            )
            if not response or not response.content:
                raise RuntimeError("empty response")
            summary = response.content
            self.store.append_history(
                summary,
                max_chars=_ARCHIVE_SUMMARY_MAX_CHARS,
                session_key=session_key,
            )
            return summary
        except Exception:
            self.store.raw_archive(messages, session_key=session_key)
            return None

    async def maybe_consolidate_by_tokens(self, session: Session, *, runtime: Runtime, replay_max_messages: int | None = None) -> None:
        if not runtime or not runtime.context_window_tokens or runtime.context_window_tokens <= 0:
            return
        lock = self.get_lock(session.key)
        async with lock:
            fresh = self.sessions.get_or_create(session.key)
            if fresh is not session:
                session = fresh
            if not session.messages:
                return

            budget = self._input_token_budget(runtime)
            target = int(budget * self.consolidation_ratio)
            last_summary = None

            try:
                estimated = sum(estimate_message_tokens(m) for m in self._full_unconsolidated_history(session))
            except Exception:
                estimated = 0
            if estimated <= 0:
                self._persist_last_summary(session, last_summary)
                return
            if estimated < budget:
                self._persist_last_summary(session, last_summary)
                return

            for round_num in range(_MAX_CONSOLIDATION_ROUNDS):
                if estimated <= target:
                    break
                boundary = self.pick_consolidation_boundary(session, max(1, estimated - target))
                if boundary is None:
                    break
                end_idx = boundary[0]
                chunk = session.messages[session.last_consolidated:end_idx]
                if not chunk:
                    break
                summary = await self.archive(chunk, runtime=runtime, session_key=session.key)
                if summary:
                    last_summary = summary
                session.last_consolidated = end_idx
                self.sessions.save(session)
                if not summary:
                    break
                try:
                    estimated = sum(estimate_message_tokens(m) for m in self._full_unconsolidated_history(session))
                except Exception:
                    break
                if estimated <= 0:
                    break

            self._persist_last_summary(session, last_summary)
            self.sessions.save(session)

    async def compact_idle_session(self, session_key: str, *, runtime: Runtime, max_suffix: int = 8) -> str:
        """Hard-truncate an idle session under the consolidation lock.

        Returns the summary text on success, None if the LLM failed
        (raw_archive fallback), or "" if there was nothing to archive.
        """
        lock = self.get_lock(session_key)
        async with lock:
            self.sessions.invalidate(session_key)
            session = self.sessions.get_or_create(session_key)

            messages_to_summarize = list(session.messages[session.last_consolidated:])
            if not messages_to_summarize:
                self.sessions.save(session)
                return ""

            probe = Session(
                key=session.key,
                messages=messages_to_summarize.copy(),
                created_at=session.created_at,
                updated_at=session.updated_at,
                metadata={},
                last_consolidated=0,
            )
            result = probe.retain_recent_legal_suffix(max_suffix, extend_to_user=True)
            messages_to_keep = probe.messages
            messages_to_remove = result.dropped[result.already_consolidated_count:]

            if not messages_to_remove and not messages_to_keep:
                self.sessions.save(session)
                return ""

            last_active = session.updated_at
            summary: str | None = ""
            if messages_to_remove:
                summary = await self.archive(
                    messages_to_remove,
                    runtime=runtime,
                    session_key=session_key,
                    summary_messages=messages_to_summarize,
                )

            if summary and summary != "(nothing)":
                session.metadata["_last_summary"] = {
                    "text": summary,
                    "last_active": last_active,
                }

            session.messages = messages_to_keep
            session.last_consolidated = 0
            self.sessions.save(session)
            return summary or ""
