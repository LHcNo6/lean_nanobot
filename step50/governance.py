from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from step50.helpers import (
    ensure_nonempty_tool_result,
    estimate_message_tokens,
    estimate_prompt_tokens_chain,
    find_legal_message_start,
    truncate_text,
)

SNIP_SAFETY_BUFFER = 1024
MICROCOMPACT_KEEP_RECENT = 10
MICROCOMPACT_MIN_CHARS = 500
INFLIGHT_COMPACT_TARGET_RATIO = 0.85
COMPACTABLE_TOOLS = frozenset({
    "exec", "grep", "find_files",
    "web_search", "web_fetch", "list_dir",
})
TOOL_RESULT_OFFLOAD_EXEMPT_TOOLS = frozenset({"read_file"})
BACKFILL_CONTENT = "[Tool result unavailable --�� call was interrupted or lost]"
PLACEHOLDER_TEXTS = frozenset({
    "[Previous assistant message omitted.]",
})


def _tool_call_name_is_valid(tool_call: Any) -> bool:
    if not isinstance(tool_call, dict):
        return False
    fn = tool_call.get("function")
    name = fn.get("name") if isinstance(fn, dict) else tool_call.get("name")
    return isinstance(name, str) and bool(name)


@dataclass
class ContextGovernanceConfig:
    tools: Any
    context_window_tokens: int | None = None
    context_block_limit: int | None = None
    max_tokens: int = 4096
    max_tool_result_chars: int = 16_000
    workspace: str | None = None
    session_key: str | None = None
    inflight_start_index: int = 0


class ContextGovernor:
    def prepare_for_model(
        self,
        config: ContextGovernanceConfig,
        messages: list[dict[str, Any]],
        compacted_tool_call_ids: set[str],
    ) -> list[dict[str, Any]]:
        updated = self.strip_placeholder_assistant_messages(messages)
        updated = self.strip_malformed_tool_calls(updated)
        updated = self.drop_orphan_tool_results(updated)
        updated = self.backfill_missing_tool_results(updated)
        updated = self.apply_tool_result_budget(config, updated)
        updated = self.compact_inflight_overflow(config, updated, compacted_tool_call_ids)
        updated = self.snip_history(config, updated)
        updated = self.drop_orphan_tool_results(updated)
        return self.backfill_missing_tool_results(updated)

    @staticmethod
    def input_budget(config: ContextGovernanceConfig) -> int:
        if not config.context_window_tokens:
            return 0
        budget = config.context_block_limit or (
            config.context_window_tokens - config.max_tokens - SNIP_SAFETY_BUFFER
        )
        return budget if budget > 0 else 0

    @staticmethod
    def normalize_tool_result(
        config: ContextGovernanceConfig,
        tool_call_id: str,
        tool_name: str,
        result: Any,
    ) -> Any:
        result = ensure_nonempty_tool_result(tool_name, result)
        if tool_name in TOOL_RESULT_OFFLOAD_EXEMPT_TOOLS:
            return result
        if isinstance(result, str) and len(result) > config.max_tool_result_chars:
            return truncate_text(result, config.max_tool_result_chars)
        return result

    @staticmethod
    def strip_placeholder_assistant_messages(
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        updated: list[dict[str, Any]] | None = None
        for idx, msg in enumerate(messages):
            if msg.get("role") != "assistant":
                if updated is not None:
                    updated.append(msg)
                continue
            content = msg.get("content", "")
            text = content if isinstance(content, str) else ""
            is_placeholder = text.strip() in PLACEHOLDER_TEXTS
            has_tool_calls = bool(msg.get("tool_calls"))
            if is_placeholder and not has_tool_calls:
                if updated is None:
                    updated = list(messages[:idx])
                continue
            if updated is not None:
                updated.append(msg)
        return updated if updated is not None else messages

    @staticmethod
    def strip_malformed_tool_calls(
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        updated: list[dict[str, Any]] | None = None
        for idx, msg in enumerate(messages):
            if msg.get("role") != "assistant":
                if updated is not None:
                    updated.append(msg)
                continue
            calls = msg.get("tool_calls")
            if not calls:
                if updated is not None:
                    updated.append(msg)
                continue
            kept = [tc for tc in calls if _tool_call_name_is_valid(tc)]
            if len(kept) == len(calls):
                if updated is not None:
                    updated.append(msg)
                continue
            if updated is None:
                updated = [dict(m) for m in messages[:idx]]
            repaired = dict(msg)
            if kept:
                repaired["tool_calls"] = kept
            else:
                repaired.pop("tool_calls", None)
            has_content = bool(repaired.get("content"))
            if not kept and not has_content:
                continue
            updated.append(repaired)
        return updated if updated is not None else messages

    @staticmethod
    def drop_orphan_tool_results(
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        declared: set[str] = set()
        updated: list[dict[str, Any]] | None = None
        for idx, msg in enumerate(messages):
            role = msg.get("role")
            if role == "assistant":
                for tc in msg.get("tool_calls") or []:
                    if isinstance(tc, dict) and tc.get("id"):
                        declared.add(str(tc["id"]))
            if role == "tool":
                tid = msg.get("tool_call_id")
                if tid and str(tid) not in declared:
                    if updated is None:
                        updated = [dict(m) for m in messages[:idx]]
                    continue
            if updated is not None:
                updated.append(dict(msg))
        return updated if updated is not None else messages

    @staticmethod
    def backfill_missing_tool_results(
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        declared: list[tuple[int, str, str]] = []
        fulfilled: set[str] = set()
        for idx, msg in enumerate(messages):
            role = msg.get("role")
            if role == "assistant":
                for tc in msg.get("tool_calls") or []:
                    if isinstance(tc, dict) and tc.get("id"):
                        name = ""
                        func = tc.get("function")
                        if isinstance(func, dict):
                            name = func.get("name", "")
                        declared.append((idx, str(tc["id"]), name))
            elif role == "tool":
                tid = msg.get("tool_call_id")
                if tid:
                    fulfilled.add(str(tid))

        missing = [(ai, cid, name) for ai, cid, name in declared if cid not in fulfilled]
        if not missing:
            return messages

        updated = list(messages)
        offset = 0
        for assistant_idx, call_id, name in missing:
            insert_at = assistant_idx + 1 + offset
            while insert_at < len(updated) and updated[insert_at].get("role") == "tool":
                insert_at += 1
            updated.insert(insert_at, {
                "role": "tool",
                "tool_call_id": call_id,
                "name": name,
                "content": BACKFILL_CONTENT,
            })
            offset += 1
        return updated

    def apply_tool_result_budget(
        self,
        config: ContextGovernanceConfig,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        updated = messages
        for idx, message in enumerate(messages):
            if message.get("role") != "tool":
                continue
            normalized = self.normalize_tool_result(
                config,
                str(message.get("tool_call_id") or f"tool_{idx}"),
                str(message.get("name") or "tool"),
                message.get("content"),
            )
            if normalized != message.get("content"):
                if updated is messages:
                    updated = [dict(m) for m in messages]
                updated[idx]["content"] = normalized
        return updated

    def compact_inflight_overflow(
        self,
        config: ContextGovernanceConfig,
        messages: list[dict[str, Any]],
        compacted_tool_call_ids: set[str],
    ) -> list[dict[str, Any]]:
        budget = self.input_budget(config)
        if budget <= 0:
            return messages

        tools = config.tools.get_definitions()
        updated = self._apply_recorded_compactions(messages, compacted_tool_call_ids)
        estimate, _ = estimate_prompt_tokens_chain(
            None, None, updated, tools,
        )
        if estimate <= budget:
            return updated

        target = int(budget * INFLIGHT_COMPACT_TARGET_RATIO)
        candidates = self._inflight_compaction_candidates(
            config, updated, compacted_tool_call_ids,
        )
        if not candidates:
            return updated

        for candidate_idx, (idx, tool_call_id) in enumerate(candidates):
            is_newest_candidate = candidate_idx == len(candidates) - 1
            if is_newest_candidate and estimate <= budget:
                break
            if tool_call_id in compacted_tool_call_ids:
                continue
            if updated is messages:
                updated = [dict(m) for m in messages]
            compacted_tool_call_ids.add(tool_call_id)
            updated[idx]["content"] = self._summary_for(updated[idx])
            estimate, _ = estimate_prompt_tokens_chain(
                None, None, updated, tools,
            )
            if estimate <= target:
                break

        return updated

    def snip_history(
        self,
        config: ContextGovernanceConfig,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not messages or not config.context_window_tokens:
            return messages

        budget = self.input_budget(config)
        if budget <= 0:
            return messages

        tools = config.tools.get_definitions()
        estimate, _ = estimate_prompt_tokens_chain(None, None, messages, tools)
        if estimate <= budget:
            return messages

        system_messages = [dict(msg) for msg in messages if msg.get("role") == "system"]
        non_system = [dict(msg) for msg in messages if msg.get("role") != "system"]
        if not non_system:
            return messages

        system_tokens = sum(estimate_message_tokens(msg) for msg in system_messages)
        remaining_budget = max(0, budget - system_tokens)
        kept: list[dict[str, Any]] = []
        kept_tokens = 0
        for message in reversed(non_system):
            msg_tokens = estimate_message_tokens(message)
            if kept and kept_tokens + msg_tokens > remaining_budget:
                break
            kept.append(message)
            kept_tokens += msg_tokens
        kept.reverse()

        result = system_messages + self._legal_history_tail(kept, non_system)
        return result

    @staticmethod
    def _summary_for(message: dict[str, Any]) -> str:
        name = message.get("name", "tool")
        return f"[Prior {name} result compacted to fit context; the tool call already completed.]"

    @staticmethod
    def _legal_history_tail(
        kept: list[dict[str, Any]],
        non_system: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        fallback = kept if kept else (non_system[-1:] if non_system else [])
        result = kept
        if not result:
            result = fallback
        result = ContextGovernor._user_tail(result) or ContextGovernor._user_tail(non_system, last=True) or fallback
        start = find_legal_message_start(result)
        return result[start:] if start else result

    @staticmethod
    def _user_tail(messages: list[dict[str, Any]], *, last: bool = False) -> list[dict[str, Any]]:
        indexes = range(len(messages) - 1, -1, -1) if last else range(len(messages))
        for idx in indexes:
            if messages[idx].get("role") == "user":
                return messages[idx:]
        return []

    @staticmethod
    def _apply_recorded_compactions(
        messages: list[dict[str, Any]],
        compacted_tool_call_ids: set[str],
    ) -> list[dict[str, Any]]:
        if not compacted_tool_call_ids:
            return messages
        updated = messages
        for idx, msg in enumerate(messages):
            if msg.get("role") != "tool":
                continue
            tool_call_id = msg.get("tool_call_id")
            if not tool_call_id or str(tool_call_id) not in compacted_tool_call_ids:
                continue
            summary = ContextGovernor._summary_for(msg)
            if msg.get("content") == summary:
                continue
            if updated is messages:
                updated = [dict(m) for m in messages]
            updated[idx]["content"] = summary
        return updated

    def _inflight_compaction_candidates(
        self,
        config: ContextGovernanceConfig,
        messages: list[dict[str, Any]],
        compacted_tool_call_ids: set[str],
    ) -> list[tuple[int, str]]:
        compactable: list[tuple[int, str]] = []
        for idx, msg in enumerate(messages):
            if idx < config.inflight_start_index:
                continue
            if msg.get("role") != "tool" or msg.get("name") not in COMPACTABLE_TOOLS:
                continue
            tool_call_id = msg.get("tool_call_id")
            if not tool_call_id or str(tool_call_id) in compacted_tool_call_ids:
                continue
            content = msg.get("content")
            if not isinstance(content, str) or len(content) < MICROCOMPACT_MIN_CHARS:
                continue
            compactable.append((idx, str(tool_call_id)))

        if not compactable:
            return []
        primary_count = max(0, len(compactable) - MICROCOMPACT_KEEP_RECENT)
        primary = compactable[:primary_count]
        fallback = compactable[primary_count:]
        return primary + fallback
