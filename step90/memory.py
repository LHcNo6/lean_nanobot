from __future__ import annotations

import json
import os
import threading
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any

from step90.helpers import truncate_text

_RAW_ARCHIVE_MAX_CHARS = 16_000
_ARCHIVE_SUMMARY_MAX_CHARS = 8_000
_HISTORY_ENTRY_HARD_CAP = 64_000
_DEFAULT_MAX_HISTORY = 1000
_DREAM_FILE_EMBED_CAP = 8000
_DREAM_CONTENT_PATHS = ("SOUL.md", "USER.md", "memory/MEMORY.md")


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


class MemoryStore:
    def __init__(self, workspace: str, max_history_entries: int = 1000) -> None:
        ws = Path(workspace)
        self.workspace = ws
        self.max_history_entries = max_history_entries
        self.memory_dir = ensure_dir(ws / "memory")
        self.memory_file = self.memory_dir / "MEMORY.md"
        self.history_file = self.memory_dir / "history.jsonl"
        self.soul_file = ws / "SOUL.md"
        self.user_file = ws / "USER.md"
        self._cursor_file = self.memory_dir / ".cursor"
        self._dream_cursor_file = self.memory_dir / ".dream_cursor"
        self._append_lock = threading.Lock()

    def append_history(self, entry: str, *, max_chars: int | None = None, session_key: str | None = None) -> int:
        limit = max_chars if max_chars is not None else _HISTORY_ENTRY_HARD_CAP
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        raw = entry.rstrip()
        if len(raw) > limit:
            raw = truncate_text(raw, limit)
        with self._append_lock:
            cursor = self._next_cursor()
            record = {"cursor": cursor, "timestamp": ts, "content": raw}
            if session_key:
                record["session_key"] = session_key
            with open(self.history_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._cursor_file.write_text(str(cursor), encoding="utf-8")
        return cursor

    def read_unprocessed_history(self, since_cursor: int) -> list[dict[str, Any]]:
        return [e for e in self._read_entries() if isinstance(e.get("cursor"), int) and e["cursor"] > since_cursor]

    def raw_archive(self, messages: list[dict[str, Any]], *, max_chars: int | None = None, session_key: str | None = None) -> int:
        limit = max_chars if max_chars is not None else _RAW_ARCHIVE_MAX_CHARS
        text = self._format_messages(messages)
        if len(text) > limit:
            text = truncate_text(text, limit)
        return self.append_history(f"[RAW] {text}", session_key=session_key)

    def compact_history(self) -> None:
        if self.max_history_entries <= 0:
            return
        entries = self._read_entries()
        if len(entries) <= self.max_history_entries:
            return
        kept = entries[-self.max_history_entries:]
        self._write_entries(kept)

    def get_last_dream_cursor(self) -> int:
        if self._dream_cursor_file.exists():
            with suppress(ValueError, OSError):
                return int(self._dream_cursor_file.read_text(encoding="utf-8").strip())
        return 0

    def set_last_dream_cursor(self, cursor: int) -> None:
        self._dream_cursor_file.write_text(str(cursor), encoding="utf-8")

    def get_latest_cursor(self) -> int:
        cursor = self._read_cursor_counter()
        if cursor is not None and cursor > 0:
            return cursor
        last = self._read_last_entry()
        if isinstance(last, dict) and isinstance(last.get("cursor"), int):
            return last["cursor"]
        return 0

    def build_dream_prompt(self, *, max_entries: int = 20) -> tuple[str, int] | None:
        last_cursor = self.get_last_dream_cursor()
        entries = self.read_unprocessed_history(since_cursor=last_cursor)
        if not entries:
            return None
        batch = entries[:max_entries]
        history_text = "\n".join(
            f"[{e['timestamp']}] {truncate_text(e['content'], 500)}"
            for e in batch
        )
        files_section = self._render_current_memory_files()
        prompt = (
            "You are a memory curator. Review conversation summaries and "
            "update the bot's memory files (SOUL.md, USER.md, memory/MEMORY.md) "
            "to reflect new facts, preferences, and decisions.\n\n"
            f"{files_section}\n\n"
            f"## Conversation History\n{history_text}"
        )
        return (prompt, batch[-1]["cursor"])

    def _render_current_memory_files(self) -> str:
        files = [
            ("SOUL.md", self.soul_file),
            ("USER.md", self.user_file),
            ("memory/MEMORY.md", self.memory_file),
        ]
        blocks = []
        for label, path in files:
            try:
                content = path.read_text(encoding="utf-8") if path.exists() else ""
            except OSError:
                content = ""
            if len(content) > _DREAM_FILE_EMBED_CAP:
                content = truncate_text(content, _DREAM_FILE_EMBED_CAP) + "\n...[truncated]"
            blocks.append(f"### {label}\n{content}" if content.strip() else f"### {label}\n(empty)")
        return "## Current Memory Files\n" + "\n\n".join(blocks)

    def _read_entries(self) -> list[dict[str, Any]]:
        entries = []
        with suppress(FileNotFoundError):
            with open(self.history_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            entries.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
        return entries

    def _write_entries(self, entries: list[dict[str, Any]]) -> None:
        with open(self.history_file, "w", encoding="utf-8") as f:
            for entry in entries:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _next_cursor(self) -> int:
        cursor = self._read_cursor_counter()
        last = self._read_last_entry()
        last_c = last.get("cursor") if isinstance(last, dict) else None
        if isinstance(last_c, int) and last_c >= 0:
            if cursor is not None:
                return max(cursor, last_c) + 1
            return last_c + 1
        all_cursors = [e.get("cursor", 0) for e in self._read_entries() if isinstance(e.get("cursor"), int)]
        max_found = max(all_cursors) if all_cursors else 0
        return max(max_found, cursor if cursor else 0) + 1

    def _read_cursor_counter(self) -> int | None:
        if not self._cursor_file.exists():
            return None
        with suppress(ValueError, OSError):
            c = int(self._cursor_file.read_text(encoding="utf-8").strip())
            if c >= 0:
                return c
        return None

    def _read_last_entry(self) -> dict[str, Any] | None:
        try:
            with open(self.history_file, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                if size == 0:
                    return None
                read_size = min(size, 4096)
                f.seek(size - read_size)
                data = f.read().decode("utf-8")
                for line in reversed(data.splitlines()):
                    line = line.strip()
                    if line:
                        return json.loads(line)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
        return None

    @staticmethod
    def _format_messages(messages: list[dict[str, Any]]) -> str:
        lines = []
        for m in messages:
            role = m.get("role", "unknown")
            content = m.get("content", "") or ""
            tc = m.get("tool_calls")
            name = m.get("name", "")
            extra = ""
            if tc:
                extra += f"\n[tool_calls: {json.dumps(tc, ensure_ascii=False)[:200]}]"
            if name:
                extra += f"\n[tool_result for tool: {name}]"
            lines.append(f"[{role}]\n{content}{extra}")
        return "\n\n---\n\n".join(lines)
