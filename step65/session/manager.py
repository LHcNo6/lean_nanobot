from __future__ import annotations

import base64
import json
import os
import re
import shutil
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from weakref import WeakValueDictionary

from step65.helpers import find_legal_message_start, recent_message_start_index


def _json_default(obj: Any) -> Any:
    """JSON 序列化默认处理：datetime 转为 ISO 字符串（step64）。"""
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Object of type {obj.__class__.__name__} is not JSON serializable")

FILE_MAX_MESSAGES = 2000
MIN_REPLAY_MAX_MESSAGES = 120
REPLAY_TOKENS_PER_MESSAGE = 100
SESSION_CACHE_MAX_SIZE = 128
_FORK_VOLATILE_METADATA_KEYS = {
    "goal_state",
    "pending_user_turn",
    "_goal_continuation_rounds",
}

_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*]')


def replay_max_messages_for_context(context_window_tokens: int | None) -> int:
    """根据 context window 大小计算回放最大消息数。

    对齐 nanobot ``session/manager.py:replay_max_messages_for_context``。

    公式：``min(FILE_MAX_MESSAGES, max(MIN_REPLAY_MAX_MESSAGES, context_window_tokens // REPLAY_TOKENS_PER_MESSAGE))``

    Args:
        context_window_tokens: 模型上下文窗口大小（None 或 <=0 表示不限制）。

    Returns:
        回放最大消息数。
    """
    if not context_window_tokens or context_window_tokens <= 0:
        return FILE_MAX_MESSAGES
    return min(
        FILE_MAX_MESSAGES,
        max(MIN_REPLAY_MAX_MESSAGES, context_window_tokens // REPLAY_TOKENS_PER_MESSAGE),
    )


def safe_filename(name: str) -> str:
    return _UNSAFE_CHARS.sub("_", name).strip()


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass
class RetentionResult:
    dropped: list[dict]
    already_consolidated_count: int


@dataclass
class Session:
    key: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    metadata: dict[str, Any] = field(default_factory=dict)
    last_consolidated: int = 0

    def __post_init__(self) -> None:
        # An out-of-range offset (corrupt metadata) would hide all history; reset it.
        if (
            isinstance(self.last_consolidated, bool)
            or not isinstance(self.last_consolidated, int)
            or not 0 <= self.last_consolidated <= len(self.messages)
        ):
            self.last_consolidated = 0

    def add_message(
        self, role: str, content: str | None, **kwargs: Any
    ) -> dict[str, Any]:
        msg: dict[str, Any] = {
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat(),
            **kwargs,
        }
        self.messages.append(msg)
        self.updated_at = datetime.now().isoformat()
        return msg

    def import_messages(self, messages: list[dict[str, Any]]) -> None:
        now = datetime.now().isoformat()
        for msg in messages:
            if "timestamp" not in msg:
                msg = dict(msg)
                msg["timestamp"] = now
            self.messages.append(msg)
        self.updated_at = now

    def get_history(
        self,
        max_messages: int = 50,
        *,
        max_tokens: int = 0,
        extend_to_user: bool = False,
        include_runtime_context: bool = True,
    ) -> list[dict[str, Any]]:
        """返回未归档消息用于 LLM 输入。

        对齐 nanobot ``Session.get_history``（step33 增强版）。

        处理顺序：
        1. 取未归档 tail（``self.messages[self.last_consolidated:]``）。
        2. 按 ``max_messages`` 切片：用 ``recent_message_start_index``（支持 ``extend_to_user``）。
        3. 避免从 turn 中间开始：从切片开头找第一个 user，若前一个是 ``_channel_delivery`` 则包含。
        4. ``find_legal_message_start`` 丢弃开头孤立的 tool 结果。
        5. 逐条处理：
           - 跳过 ``_command`` 消息；
           - ``include_runtime_context=False`` 时调用 ``public_history_message`` 移除运行时上下文；
           - 空 assistant 消息（无 tool_calls / reasoning_content / thinking_blocks）跳过；
           - 只保留字段白名单：role / content / tool_calls / tool_call_id / name。
        6. ``max_tokens`` 预算：从尾部累加，超出预算则截断。
        7. token 预算后 user turn 对齐：找第一个 user 从 user 开始保留；
           若无 user，从原始 out 中恢复最近的 user（即使略超预算）。

        step33 简化边界：
        - 不做 media / cli_apps breadcrumb（需要媒体处理基础设施）。
        - 不做 ``_sanitize_assistant_replay_text``（留待后续研究）。

        Args:
            max_messages: 最大消息数（<=0 表示不限制，使用 FILE_MAX_MESSAGES）。
            max_tokens: token 预算（0 表示不限制）。
            extend_to_user: 切片时是否向前扩展到最近的 user turn。
            include_runtime_context: 是否包含运行时上下文（False 时调用 public_history_message）。

        Returns:
            处理后的消息列表（深拷贝，不影响 session 内部存储）。
        """
        from step65.helpers import estimate_message_tokens
        from step65.runtime_context import RUNTIME_CONTEXT_HISTORY_META, public_history_message

        unconsolidated = self.messages[self.last_consolidated:]
        effective_max = max_messages if max_messages > 0 else FILE_MAX_MESSAGES

        # 1. 按 max_messages 切片（支持 extend_to_user）
        start_idx = recent_message_start_index(
            unconsolidated,
            effective_max,
            extend_to_user=extend_to_user,
        )
        sliced = unconsolidated[start_idx:]

        # 2. 避免从 turn 中间开始：找到第一个 user，若前一个是 _channel_delivery 则包含
        for i, message in enumerate(sliced):
            if message.get("role") == "user":
                start = i
                if i > 0 and sliced[i - 1].get("_channel_delivery"):
                    start = i - 1
                sliced = sliced[start:]
                break

        # 3. 丢弃开头孤立的 tool 结果
        legal_start = find_legal_message_start(sliced)
        if legal_start:
            sliced = sliced[legal_start:]

        # 4. 逐条处理
        out: list[dict[str, Any]] = []
        for message in sliced:
            # 跳过 _command 消息
            if message.get("_command"):
                continue

            # include_runtime_context=False 时移除运行时上下文
            if not include_runtime_context:
                message = public_history_message(message)

            content = message.get("content", "")
            role = message.get("role")

            # 空 assistant 消息过滤：无 tool_calls / reasoning_content / thinking_blocks 则跳过
            if role == "assistant" and isinstance(content, str) and not content.strip():
                if not any(
                    key in message
                    for key in ("tool_calls", "reasoning_content", "thinking_blocks")
                ):
                    continue

            # 字段白名单：只保留 LLM 输入需要的字段
            entry: dict[str, Any] = {"role": message["role"], "content": content}
            for key in ("tool_calls", "tool_call_id", "name"):
                if key in message:
                    entry[key] = message[key]
            out.append(entry)

        # 5. max_tokens 预算：从尾部累加
        if max_tokens > 0 and out:
            kept: list[dict[str, Any]] = []
            used = 0
            for message in reversed(out):
                tokens = estimate_message_tokens(message)
                if kept and used + tokens > max_tokens:
                    break
                kept.append(message)
                used += tokens
            kept.reverse()

            # 6. token 预算后 user turn 对齐
            first_user = next(
                (i for i, m in enumerate(kept) if m.get("role") == "user"),
                None,
            )
            if first_user is not None:
                kept = kept[first_user:]
            else:
                # 紧 token 预算可能留下 assistant-only tail；若原始 out 中有 user，恢复最近的一个
                recovered_user = next(
                    (i for i in range(len(out) - 1, -1, -1) if out[i].get("role") == "user"),
                    None,
                )
                if recovered_user is not None:
                    kept = out[recovered_user:]
            out = kept

        return out

    def get_public_history(
        self, max_messages: int = 50, max_tokens: int = 0
    ) -> list[dict[str, Any]]:
        """返回用户可见的历史（运行时上下文已移除，隐藏行已过滤）。

        对齐 nanobot ``Session.get_history(public=True)`` 的语义：
        1. 先从原始消息中过滤 ``_hidden_history`` 标记的消息（如 subagent 内部注入）；
        2. 调用 ``get_history(include_runtime_context=False)`` 取未归档消息（运行时上下文已移除）。

        注意：step33 变更——由于 ``get_history`` 的字段白名单会移除 ``_hidden_history``
        元数据，因此必须在调用 ``get_history`` **之前**先过滤隐藏消息，否则无法识别。

        注意：与 ``get_history`` 不同，本方法返回的是**深拷贝**，不会影响
        session 内部存储。
        """
        from step65.session.history_visibility import is_hidden_history_message

        # 先过滤隐藏消息（必须在 get_history 之前，因为字段白名单会移除 _hidden_history）
        filtered_messages = [
            msg for msg in self.messages
            if not is_hidden_history_message(msg)
        ]
        # 创建临时 Session 用于调用 get_history
        temp_session = Session(
            key=self.key,
            messages=filtered_messages,
            created_at=self.created_at,
            updated_at=self.updated_at,
            metadata=dict(self.metadata),
            last_consolidated=min(self.last_consolidated, len(filtered_messages)),
        )
        return temp_session.get_history(
            max_messages=max_messages,
            max_tokens=max_tokens,
            include_runtime_context=False,
        )

    def clear(self) -> None:
        """Clear all messages and reset session to initial state."""
        self.messages = []
        self.last_consolidated = 0
        self.updated_at = datetime.now().isoformat()
        self.metadata.pop("_last_summary", None)

    def retain_recent_legal_suffix(
        self,
        max_messages: int,
        *,
        extend_to_user: bool = False,
    ) -> RetentionResult:
        """Keep a legal recent suffix, optionally extending it back to a user turn.

        Returns a RetentionResult with dropped messages and how many of those
        were in the already-consolidated prefix. This method mutates
        self.messages and self.last_consolidated in place.
        """
        if max_messages <= 0:
            dropped = list(self.messages)
            lc = self.last_consolidated
            self.clear()
            return RetentionResult(
                dropped=dropped,
                already_consolidated_count=min(lc, len(dropped)),
            )
        if len(self.messages) <= max_messages:
            return RetentionResult(
                dropped=[],
                already_consolidated_count=0,
            )

        original = list(self.messages)
        before_lc = self.last_consolidated

        start_idx = max(0, len(self.messages) - max_messages)
        if extend_to_user:
            start_idx = next(
                (i for i in range(start_idx, -1, -1) if self.messages[i].get("role") == "user"),
                start_idx,
            )

        retained = self.messages[start_idx:]

        # Prefer starting at a user turn when one exists within the retained window.
        first_user = next((i for i, m in enumerate(retained) if m.get("role") == "user"), None)
        if first_user is not None:
            retained = retained[first_user:]
        elif not extend_to_user:
            # If the hard-capped tail is assistant/tool-only, anchor to the
            # latest user in the full session and take a capped forward window.
            latest_user = next(
                (i for i in range(len(self.messages) - 1, -1, -1)
                 if self.messages[i].get("role") == "user"),
                None,
            )
            if latest_user is not None:
                retained = self.messages[latest_user: latest_user + max_messages]

        # Mirror get_history(): avoid persisting orphan tool results at the front.
        start = find_legal_message_start(retained)
        if start:
            retained = retained[start:]

        # Hard-cap guarantee unless the caller requested user-turn extension.
        if not extend_to_user and len(retained) > max_messages:
            retained = retained[-max_messages:]
            start = find_legal_message_start(retained)
            if start:
                retained = retained[start:]

        # Compute actually-dropped messages using identity comparison so that
        # even when retained is a non-contiguous slice of original (the else
        # branch above), we never duplicate or lose messages.
        retained_ids = set(id(m) for m in retained)
        dropped = [m for m in original if id(m) not in retained_ids]

        # Count how many dropped messages were in the already-consolidated
        # prefix of the original list.
        already_consolidated = sum(
            1 for i, m in enumerate(original)
            if i < before_lc and id(m) not in retained_ids
        )

        # New last_consolidated = count of retained messages that were inside
        # the old consolidated prefix.
        new_lc = sum(
            1 for i, m in enumerate(original)
            if i < before_lc and id(m) in retained_ids
        )

        self.messages = retained
        self.last_consolidated = new_lc
        self.updated_at = datetime.now().isoformat()
        return RetentionResult(
            dropped=dropped,
            already_consolidated_count=already_consolidated,
        )

    def enforce_file_cap(
        self,
        on_archive: Any = None,
        limit: int = FILE_MAX_MESSAGES,
    ) -> None:
        """Bound session message growth by archiving and trimming old prefixes."""
        if limit <= 0 or len(self.messages) <= limit:
            return

        result = self.retain_recent_legal_suffix(limit)
        if not result.dropped:
            return

        archive_chunk = result.dropped[result.already_consolidated_count:]
        if archive_chunk and on_archive:
            on_archive(archive_chunk)


class SessionManager:
    """Manages conversation sessions stored as JSONL files."""

    def __init__(self, workspace: str = ".", *, max_cached_sessions: int = SESSION_CACHE_MAX_SIZE):
        self.sessions_dir = ensure_dir(Path(workspace) / "sessions")
        self._cache: OrderedDict[str, Session] = OrderedDict()
        # Preserve identity for sessions held by active callers without retaining idle ones.
        self._overflow_cache: WeakValueDictionary[str, Session] = WeakValueDictionary()
        self._max_cached_sessions = max_cached_sessions

    def _remember(self, session: Session) -> None:
        """Keep recent sessions strongly cached without duplicating live objects."""
        self._overflow_cache.pop(session.key, None)
        self._cache[session.key] = session
        self._cache.move_to_end(session.key)
        while len(self._cache) > self._max_cached_sessions:
            key, evicted = self._cache.popitem(last=False)
            self._overflow_cache[key] = evicted

    def _cached(self, key: str) -> Session | None:
        session = self._cache.get(key)
        if session is not None:
            self._cache.move_to_end(key)
            return session

        session = self._overflow_cache.get(key)
        if session is not None:
            self._remember(session)
        return session

    @staticmethod
    def safe_key(key: str) -> str:
        """Public helper: map an arbitrary key to a stable filename stem."""
        return safe_filename(key.replace(":", "_"))

    @staticmethod
    def _storage_key(key: str) -> str:
        """Collision-resistant encoding for internal session storage filenames."""
        return base64.urlsafe_b64encode(key.encode()).decode().rstrip("=")

    @staticmethod
    def _decode_storage_key(stem: str) -> str | None:
        """Reverse _storage_key(): decode a base64url (no-padding) stem back to the original key."""
        try:
            # Restore padding stripped by rstrip("=")
            padding = 4 - len(stem) % 4
            if padding != 4:
                stem += "=" * padding
            return base64.urlsafe_b64decode(stem).decode("utf-8")
        except Exception:
            return None

    def _get_session_path(self, key: str) -> Path:
        """Get the collision-resistant workspace path for a session."""
        return self.sessions_dir / f"{self._storage_key(key)}.jsonl"

    def _get_legacy_lossy_path(self, key: str) -> Path:
        """Previous workspace session path using lossy ':' to '_' replacement."""
        return self.sessions_dir / f"{self.safe_key(key)}.jsonl"

    @staticmethod
    def _stored_key_for_path(path: Path) -> str | None:
        """Read the stored session key from a JSONL metadata row, if present."""
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    data = json.loads(line)
                    if data.get("_type") == "metadata":
                        stored_key = data.get("key")
                        return stored_key if isinstance(stored_key, str) else None
                    return None
        except Exception:
            return None
        return None

    def get_or_create(self, key: str) -> Session:
        session = self._cached(key)
        if session is not None:
            return session

        session = self._load(key)
        if session is None:
            session = Session(key=key)

        self._remember(session)
        return session

    def _load(self, key: str) -> Session | None:
        path = self._get_session_path(key)
        if not path.exists():
            legacy = self._get_legacy_lossy_path(key)
            if legacy.exists():
                stored_key = self._stored_key_for_path(legacy)
                if stored_key is None or stored_key == key:
                    try:
                        shutil.move(str(legacy), str(path))
                    except OSError:
                        pass
        if not path.exists():
            return None

        try:
            messages: list[dict[str, Any]] = []
            metadata: dict[str, Any] = {}
            created_at: str | None = None
            updated_at: str | None = None
            last_consolidated = 0

            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    data = json.loads(line)
                    if data.get("_type") == "metadata":
                        metadata = data.get("metadata", {})
                        created_at = data.get("created_at")
                        updated_at = data.get("updated_at")
                        last_consolidated = data.get("last_consolidated", 0)
                    else:
                        messages.append(data)

            return Session(
                key=key,
                messages=messages,
                created_at=created_at or datetime.now().isoformat(),
                updated_at=updated_at or datetime.now().isoformat(),
                metadata=metadata,
                last_consolidated=last_consolidated,
            )
        except (json.JSONDecodeError, OSError):
            return None

    def save(self, session: Session, *, fsync: bool = False) -> None:
        path = self._get_session_path(session.key)
        tmp_path = path.with_suffix(".jsonl.tmp")

        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                metadata_line = {
                    "_type": "metadata",
                    "key": session.key,
                    "created_at": session.created_at,
                    "updated_at": session.updated_at,
                    "metadata": session.metadata,
                    "last_consolidated": session.last_consolidated,
                }
                f.write(json.dumps(metadata_line, ensure_ascii=False, default=_json_default) + "\n")
                for msg in session.messages:
                    f.write(json.dumps(msg, ensure_ascii=False, default=_json_default) + "\n")
                if fsync:
                    f.flush()
                    os.fsync(f.fileno())

            os.replace(tmp_path, path)

            if fsync:
                try:
                    fd = os.open(str(path.parent), os.O_RDONLY)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                except PermissionError:
                    pass
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

        self._remember(session)

    def invalidate(self, key: str) -> None:
        """Remove a session from the in-memory caches."""
        self._cache.pop(key, None)
        self._overflow_cache.pop(key, None)

    def fork_session_before_user_index(
        self,
        source_key: str,
        target_key: str,
        before_user_index: int,
    ) -> Session | None:
        """Create *target_key* from *source_key* before a global user-message index.

        ``before_user_index`` is zero-based over user messages in the full session:
        ``0`` means "before the first user message", ``1`` means "before the
        second user message", and so on. A value equal to the total user-message
        count copies the full session prefix.
        """
        if before_user_index < 0:
            return None
        source = self._cached(source_key) or self._load(source_key)
        if source is None:
            return None

        copied: list[dict[str, Any]] = []
        user_index = 0
        found_target = False
        for message in source.messages:
            if message.get("role") == "user":
                if user_index == before_user_index:
                    found_target = True
                    break
                user_index += 1
            copied.append(deepcopy(message))
        if user_index == before_user_index:
            found_target = True
        if not found_target:
            return None

        metadata = deepcopy(source.metadata)
        for key in _FORK_VOLATILE_METADATA_KEYS:
            metadata.pop(key, None)

        last_consolidated = min(source.last_consolidated, len(copied))
        if source.last_consolidated > len(copied):
            metadata.pop("_last_summary", None)
            last_consolidated = 0

        now = datetime.now()
        target = Session(
            key=target_key,
            messages=copied,
            created_at=now.isoformat(),
            updated_at=now.isoformat(),
            metadata=metadata,
            last_consolidated=last_consolidated,
        )
        self.save(target, fsync=True)
        return target

    def list_sessions(self) -> list[dict[str, Any]]:
        """List all sessions with key, timestamps and a text preview."""
        sessions: list[dict[str, Any]] = []

        for path in self.sessions_dir.glob("*.jsonl"):
            decoded = self._decode_storage_key(path.stem)
            fallback_key = decoded or path.stem.replace("_", ":", 1)
            try:
                with open(path, encoding="utf-8") as f:
                    first_line = f.readline().strip()
                    if not first_line:
                        continue
                    data = json.loads(first_line)
                    if data.get("_type") != "metadata":
                        continue
                    key = data.get("key") or fallback_key
                    metadata = data.get("metadata", {})
                    preview = ""
                    fallback_preview = ""
                    for line in f:
                        if not line.strip():
                            continue
                        item = json.loads(line)
                        if item.get("_type") == "metadata":
                            continue
                        text = item.get("content")
                        if not isinstance(text, str) or not text.strip():
                            continue
                        text = " ".join(text.split())
                        if len(text) > 120:
                            text = text[:119] + "…"
                        if item.get("role") == "user":
                            preview = text
                            break
                        if not fallback_preview:
                            fallback_preview = text
                    preview = preview or fallback_preview
                    fallback_time = datetime.fromtimestamp(path.stat().st_mtime).isoformat()
                    sessions.append(
                        {
                            "key": key,
                            "created_at": data.get("created_at") or fallback_time,
                            "updated_at": data.get("updated_at") or fallback_time,
                            "metadata": metadata if isinstance(metadata, dict) else {},
                            "preview": preview,
                        }
                    )
            except (json.JSONDecodeError, OSError):
                continue
        return sorted(sessions, key=lambda x: x.get("updated_at", ""), reverse=True)
