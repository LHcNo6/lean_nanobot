from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any

from step72.helpers import (
    estimate_message_tokens,
    estimate_prompt_tokens_chain,
    find_legal_message_start,
    recent_message_start_index,
    truncate_text,
)
from step72.llm import Runtime
from step72.memory import MemoryStore, _ARCHIVE_SUMMARY_MAX_CHARS, _RAW_ARCHIVE_MAX_CHARS
from step72.session import Session, SessionManager

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
        from step72.helpers import estimate_prompt_tokens
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

    @staticmethod
    def _replay_overflow_boundary(
        session: Session,
        replay_max_messages: int | None,
    ) -> int | None:
        """计算 replay overflow 压缩的结束索引。

        对齐 nanobot ``Consolidator._replay_overflow_boundary``。

        逻辑：
        1. ``replay_max_messages`` 为空或 <=0 → 返回 None（不压缩）。
        2. 未归档 tail 长度 <= replay_max_messages → 返回 None（不压缩）。
        3. 用 ``recent_message_start_index(tail, replay_max_messages, extend_to_user=True)`` 找起始。
        4. 从起始找第一个 user；若前一个是 ``_channel_delivery`` 则包含前一个。
        5. ``find_legal_message_start`` 找合法起始（丢弃孤立 tool 结果）。
        6. 返回第一个可见消息的绝对索引；若 <= last_consolidated 则返回 None。

        Args:
            session: Session 对象。
            replay_max_messages: 回放最大消息数。

        Returns:
            结束索引（绝对索引，用于切片 ``session.messages[last_consolidated:end_idx]``），
            或 None 表示不需要压缩。
        """
        if not replay_max_messages or replay_max_messages <= 0:
            return None
        tail = list(enumerate(session.messages[session.last_consolidated:], session.last_consolidated))
        if len(tail) <= replay_max_messages:
            return None

        tail_messages = [message for _idx, message in tail]
        start_idx = recent_message_start_index(
            tail_messages,
            replay_max_messages,
            extend_to_user=True,
        )
        sliced = tail[start_idx:]

        # 从起始找第一个 user；若前一个是 _channel_delivery 则包含
        for i, (_idx, message) in enumerate(sliced):
            if message.get("role") == "user":
                start = i
                if i > 0 and sliced[i - 1][1].get("_channel_delivery"):
                    start = i - 1
                sliced = sliced[start:]
                break

        # 丢弃孤立 tool 结果
        legal_start = find_legal_message_start([message for _idx, message in sliced])
        if legal_start:
            sliced = sliced[legal_start:]
        if not sliced:
            return len(session.messages)

        first_visible_idx = sliced[0][0]
        if first_visible_idx <= session.last_consolidated:
            return None
        return first_visible_idx

    async def _consolidate_replay_overflow(
        self,
        session: Session,
        replay_max_messages: int | None,
        *,
        runtime: Runtime,
    ) -> str | None:
        """归档会被回放消息窗口隐藏的消息。

        对齐 nanobot ``Consolidator._consolidate_replay_overflow``。

        当未归档消息数超过 ``replay_max_messages`` 时，将超出部分的消息
        归档为摘要，更新 ``session.last_consolidated``。

        Args:
            session: Session 对象。
            replay_max_messages: 回放最大消息数。
            runtime: LLMRuntime 对象。

        Returns:
            生成的摘要文本，或 None 表示没有需要归档的消息。
        """
        end_idx = self._replay_overflow_boundary(session, replay_max_messages)
        if end_idx is None:
            return None
        chunk = session.messages[session.last_consolidated:end_idx]
        if not chunk:
            return None
        summary = await self.archive(
            chunk,
            runtime=runtime,
            session_key=session.key,
        )
        session.last_consolidated = end_idx
        self.sessions.save(session)
        return summary

    def estimate_session_prompt_tokens(
        self,
        session: Session,
        *,
        runtime: Runtime,
    ) -> tuple[int, str]:
        """估算完整未归档 session tail 的 prompt token 数。

        对齐 nanobot ``Consolidator.estimate_session_prompt_tokens``。

        构建包含历史、当前消息占位、session_summary 的 probe_messages，
        调用 ``estimate_prompt_tokens_chain`` 估算。

        Args:
            session: Session 对象。
            runtime: LLMRuntime 对象。

        Returns:
            ``(token_count, source)`` 元组，source 表示估算来源。
        """
        history = self._full_unconsolidated_history(session)
        meta = session.metadata.get("_last_summary")
        summary = meta.get("text") if isinstance(meta, dict) else (
            meta if isinstance(meta, str) else None
        )
        probe_messages = self._build_messages(
            history=history,
            current_message="[token-probe]",
            channel=None,
            chat_id=None,
            sender_id=None,
            session_summary=summary,
            session_metadata=session.metadata,
            session_key=session.key,
            unified_session=getattr(self, "unified_session", False),
        )
        return estimate_prompt_tokens_chain(
            runtime.provider,
            runtime.model,
            probe_messages,
            self._get_tool_definitions(),
        )

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
        # step32 (A12 下半场)：摘要前先移除运行时上下文后缀，避免
        # [Runtime Context — metadata only...] 等内部标记污染摘要内容。
        from step72.runtime_context import public_history_messages
        msgs = summary_messages if summary_messages is not None else messages
        msgs = public_history_messages(msgs)
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
        """基于 token 预算和回放窗口进行压缩。

        对齐 nanobot ``Consolidator.maybe_consolidate_by_tokens``。

        处理顺序：
        1. 先调用 ``_consolidate_replay_overflow`` 归档超出回放窗口的消息。
        2. 再基于 token 预算进行多轮压缩（直到未归档 token 数 <= target）。

        Args:
            session: Session 对象。
            runtime: LLMRuntime 对象。
            replay_max_messages: 回放最大消息数（None 表示不做 replay overflow 压缩）。
        """
        if not runtime or not runtime.context_window_tokens or runtime.context_window_tokens <= 0:
            return
        lock = self.get_lock(session.key)
        async with lock:
            fresh = self.sessions.get_or_create(session.key)
            if fresh is not session:
                session = fresh
            if not session.messages:
                return

            # step33：先做 replay overflow 压缩（归档超出回放窗口的消息）
            await self._consolidate_replay_overflow(
                session,
                replay_max_messages,
                runtime=runtime,
            )

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
