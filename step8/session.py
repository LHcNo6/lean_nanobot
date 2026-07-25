from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*]')


def safe_filename(name: str) -> str:
    return _UNSAFE_CHARS.sub("_", name).strip()


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass
class Session:
    key: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    metadata: dict[str, Any] = field(default_factory=dict)
    last_consolidated: int = 0

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
        self, max_messages: int = 50, max_tokens: int = 0
    ) -> list[dict[str, Any]]:
        from step8.consolidation import estimate_message_tokens

        unconsolidated = self.messages[self.last_consolidated:]

        if max_tokens > 0:
            kept: list[dict[str, Any]] = []
            used = 0
            for msg in reversed(unconsolidated):
                tokens = estimate_message_tokens(msg)
                if kept and used + tokens > max_tokens:
                    break
                kept.append(msg)
                used += tokens
            kept.reverse()
            unconsolidated = kept

        if max_messages > 0 and len(unconsolidated) > max_messages:
            unconsolidated = unconsolidated[-max_messages:]

        return list(unconsolidated)


class SessionManager:
    def __init__(self, workspace: str = "."):
        self.sessions_dir = ensure_dir(Path(workspace) / "sessions")
        self._cache: dict[str, Session] = {}

    def _session_path(self, key: str) -> Path:
        return self.sessions_dir / f"{safe_filename(key)}.jsonl"

    def get_or_create(self, key: str) -> Session:
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        session = self._load(key)
        if session is None:
            session = Session(key=key)

        self._cache[key] = session
        return session

    def _load(self, key: str) -> Session | None:
        path = self._session_path(key)
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
        path = self._session_path(session.key)
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
                f.write(json.dumps(metadata_line, ensure_ascii=False) + "\n")
                for msg in session.messages:
                    f.write(json.dumps(msg, ensure_ascii=False) + "\n")
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

        self._cache[session.key] = session
