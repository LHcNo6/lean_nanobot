from __future__ import annotations

import json
import logging
import os
import re
import threading
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from step124.helpers import strip_think, truncate_text
from step124.utils.workspace_prompts import (
    WORKSPACE_PROMPT_MAX_CHARS,
    has_workspace_prompt_override,
    load_workspace_prompt_override,
    workspace_prompt_file,
)
from step124.utils.gitstore import GitStore

logger = logging.getLogger(__name__)

_RAW_ARCHIVE_MAX_CHARS = 16_000
_ARCHIVE_SUMMARY_MAX_CHARS = 8_000
_HISTORY_ENTRY_HARD_CAP = 64_000

# Legacy HISTORY.md 迁移相关正则
_LEGACY_ENTRY_START_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2}[^\]]*)\]\s*")
_LEGACY_TIMESTAMP_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\]\s*")
_LEGACY_RAW_MESSAGE_RE = re.compile(
    r"^\[\d{4}-\d{2}-\d{2}[^\]]*\]\s+[A-Z][A-Z0-9_]*(?:\s+\[tools:\s*[^\]]+\])?:"
)
_DEFAULT_MAX_HISTORY = 1000
_DREAM_FILE_EMBED_CAP = 8000
_DREAM_CONTENT_PATHS = ("SOUL.md", "USER.md", "memory/MEMORY.md")


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


class MemoryStore:
    _INTERNAL_HISTORY_SESSION_PREFIXES = ("cron:", "dream:")
    _INTERNAL_HISTORY_SESSION_KEYS = {"heartbeat"}

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
        self.legacy_history_file = self.memory_dir / "HISTORY.md"
        self._append_lock = threading.Lock()
        self._oversize_logged = False  # 超限条目警告限流
        self._corruption_logged = False  # 无效 cursor 警告限流
        self._malformed_entry_logged = False  # 畸形 entry 警告限流
        self._dream_prompt_oversize_logged = False  # dream prompt 超限警告限流
        # Git 版本控制（tracked 记忆文件）
        self._git = GitStore(
            workspace=self.workspace,
            tracked_files=["memory/MEMORY.md", "SOUL.md", "USER.md"],
        )

    # -- 通用文件读取 -------------------------------------------------------

    @staticmethod
    def read_file(path: Path) -> str:
        """读取文本文件内容，文件不存在时返回空字符串。

        统一处理三个持久化记忆文件（MEMORY.md / SOUL.md / USER.md）的
        "可选文件"语义：新 workspace 初始化时这些文件可能不存在，
        读取时不应抛出异常。

        Args:
            path: 要读取的文件路径。

        Returns:
            文件的 UTF-8 文本内容；文件不存在时返回空字符串。
        """
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    # -- MEMORY.md（长期记忆） ---------------------------------------------

    def read_memory(self) -> str:
        """读取长期记忆文件 MEMORY.md 的内容。

        Returns:
            MEMORY.md 的文本内容；文件不存在时返回空字符串。
        """
        return self.read_file(self.memory_file)

    def write_memory(self, content: str) -> None:
        """覆盖写入长期记忆文件 MEMORY.md。

        Args:
            content: 要写入的完整文本内容。
        """
        self.memory_file.write_text(content, encoding="utf-8")

    # -- SOUL.md（人格/灵魂） ----------------------------------------------

    def read_soul(self) -> str:
        """读取人格文件 SOUL.md 的内容。

        Returns:
            SOUL.md 的文本内容；文件不存在时返回空字符串。
        """
        return self.read_file(self.soul_file)

    def write_soul(self, content: str) -> None:
        """覆盖写入人格文件 SOUL.md。

        Args:
            content: 要写入的完整文本内容。
        """
        self.soul_file.write_text(content, encoding="utf-8")

    # -- USER.md（用户画像） -----------------------------------------------

    def read_user(self) -> str:
        """读取用户画像文件 USER.md 的内容。

        Returns:
            USER.md 的文本内容；文件不存在时返回空字符串。
        """
        return self.read_file(self.user_file)

    def write_user(self, content: str) -> None:
        """覆盖写入用户画像文件 USER.md。

        Args:
            content: 要写入的完整文本内容。
        """
        self.user_file.write_text(content, encoding="utf-8")

    # -- 上下文注入（供 context.py 使用） -----------------------------------

    def get_memory_context(self) -> str:
        """获取长期记忆上下文，用于注入 system prompt。

        读取 MEMORY.md 的内容，包装为 ``## Long-term Memory`` 段。
        MEMORY.md 为空或不存在时返回空字符串，调用方应跳过注入。

        Returns:
            格式化的长期记忆文本；无内容时返回空字符串。
        """
        long_term = self.read_memory()
        if not long_term:
            return ""
        return f"## Long-term Memory\n{long_term}"

    # -- Dream prompt 模板 ---------------------------------------------------

    @property
    def dream_prompt_file(self) -> Path:
        """返回 workspace dream prompt 覆盖文件路径。"""
        return workspace_prompt_file(self.workspace, "dream")

    def has_dream_prompt_override(self) -> bool:
        """返回是否存在非空 dream prompt 覆盖文件。"""
        return has_workspace_prompt_override(self.dream_prompt_file)

    @staticmethod
    def default_dream_prompt() -> str:
        """返回默认 dream prompt（后续 step 将改为模板渲染）。"""
        return (
            "You are Dream, a background memory consolidation agent.\n"
            "Review the recent history and update long-term memory files.\n"
            "Extract persistent facts, preferences, and project knowledge."
        )

    def _dream_template(self) -> str:
        """返回 dream prompt：优先使用 workspace 覆盖文件，否则用默认。"""
        text, original_chars = load_workspace_prompt_override(self.dream_prompt_file)
        if text is not None:
            if (
                original_chars > WORKSPACE_PROMPT_MAX_CHARS
                and not self._dream_prompt_oversize_logged
            ):
                self._dream_prompt_oversize_logged = True
                logger.warning(
                    "workspace Dream prompt exceeds %d chars (%d); truncating. "
                    "Further occurrences suppressed.",
                    WORKSPACE_PROMPT_MAX_CHARS, original_chars,
                )
            return text
        return self.default_dream_prompt()

    @property
    def git(self) -> GitStore:
        """返回 GitStore 实例。"""
        return self._git

    def dream_content_diff(self) -> str:
        """返回持久化记忆文件未提交变更的结构化摘要。

        git 不可用或无变更时返回空字符串。这是 diff-grounded Dream commit
        message 和 cursor 推进门控的真实输入（绝不依赖 LLM 自报）。
        """
        if not self._git.is_initialized():
            return ""
        return self._git.summarize_working_tree()

    def build_dream_tools(self) -> list[dict[str, Any]]:
        """构建 Dream 运行使用的受限工具定义列表。

        Dream 只能访问记忆文件相关的文件操作工具，不能执行 shell 命令
        或访问网络。返回 OpenAI 格式的工具定义列表。

        Returns:
            工具定义列表，包含 read_file、write_file、edit_file 等。
        """
        return [
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Read a file from the workspace.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "File path relative to workspace."},
                        },
                        "required": ["path"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "write_file",
                    "description": "Write content to a file in the workspace.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "File path relative to workspace."},
                            "content": {"type": "string", "description": "Content to write."},
                        },
                        "required": ["path", "content"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "edit_file",
                    "description": "Edit a file by replacing old text with new text.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "File path relative to workspace."},
                            "old_text": {"type": "string", "description": "Text to replace."},
                            "new_text": {"type": "string", "description": "Replacement text."},
                        },
                        "required": ["path", "old_text", "new_text"],
                    },
                },
            },
        ]

    def append_history(self, entry: str, *, max_chars: int | None = None, session_key: str | None = None) -> int:
        """追加历史条目到 history.jsonl，返回自增 cursor。

        写入前调用 ``strip_think`` 清理模板泄漏（未闭合的 ``<think`` 前缀、
        ``<channel|>`` 标记等）。超限条目首次 ``logger.warning`` 后限流。
        raw 非空但 strip 后为空时持久化空串（不回退到 raw，避免 undo
        strip_think 的保证）。

        Args:
            entry: 要写入的历史内容。
            max_chars: 单条最大字符数，None 时用 ``_HISTORY_ENTRY_HARD_CAP``。
            session_key: 会话键，None 时不记录。

        Returns:
            新分配的 cursor 值。
        """
        limit = max_chars if max_chars is not None else _HISTORY_ENTRY_HARD_CAP
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        raw = entry.rstrip()
        if len(raw) > limit:
            if not self._oversize_logged:
                self._oversize_logged = True
                logger.warning(
                    "history entry exceeds %d chars (%d); truncating. "
                    "Usually means a caller forgot its own cap; "
                    "further occurrences suppressed.",
                    limit, len(raw),
                )
            raw = truncate_text(raw, limit)
        content = strip_think(raw)
        # cursor 分配和追加必须原子：并发写入者可能读到相同 cursor 导致重复
        with self._append_lock:
            cursor = self._next_cursor()
            if raw and not content:
                logger.debug(
                    "history entry %d stripped to empty (likely template leak); "
                    "persisting empty content to avoid re-polluting context",
                    cursor,
                )
            record = {"cursor": cursor, "timestamp": ts, "content": content}
            if session_key:
                record["session_key"] = session_key
            with open(self.history_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._cursor_file.write_text(str(cursor), encoding="utf-8")
        return cursor

    def read_unprocessed_history(self, since_cursor: int) -> list[dict[str, Any]]:
        """返回 cursor > since_cursor 的有效历史条目。

        使用 ``_iter_valid_entries`` 过滤无效 cursor 和畸形 payload，
        防止外部写入的坏数据影响下游消费。

        Args:
            since_cursor: 起始 cursor（不含）。

        Returns:
            有效条目列表。
        """
        return [e for e, c in self._iter_valid_entries() if c > since_cursor]

    @classmethod
    def _is_internal_history_session(cls, session_key: str | None) -> bool:
        """判断会话键是否属于内部会话（cron/dream/heartbeat）。

        内部会话的历史不应注入到普通用户会话的 prompt 中。

        Args:
            session_key: 会话键，None 时返回 False。

        Returns:
            True 表示内部会话，False 表示普通会话。
        """
        if not session_key:
            return False
        return (
            session_key in cls._INTERNAL_HISTORY_SESSION_KEYS
            or session_key.startswith(cls._INTERNAL_HISTORY_SESSION_PREFIXES)
        )

    def read_recent_history_for_prompt(
        self,
        since_cursor: int,
        *,
        session_key: str | None,
        unified_session: bool = False,
    ) -> list[dict[str, Any]]:
        """返回可安全注入到 turn prompt 的未处理历史条目。

        - session_key=None：返回所有未处理历史
        - unified_session=False：只返回 session_key 完全匹配的条目
        - unified_session=True：返回 session_key 匹配的条目 + 所有非内部会话条目

        Args:
            since_cursor: 起始 cursor（不含）。
            session_key: 当前会话键，None 表示不过滤。
            unified_session: 是否合并非内部会话的跨会话历史。

        Returns:
            过滤后的历史条目列表。
        """
        entries = self.read_unprocessed_history(since_cursor=since_cursor)
        if session_key is None:
            return entries
        if not unified_session:
            return [e for e in entries if e.get("session_key") == session_key]

        return [
            entry
            for entry in entries
            if (entry_session := entry.get("session_key")) == session_key
            or not self._is_internal_history_session(entry_session)
        ]

    def raw_archive(self, messages: list[dict[str, Any]], *, max_chars: int | None = None, session_key: str | None = None) -> int:
        """回退：将原始消息转储到 history.jsonl（不经过 LLM 摘要）。

        使用 ``public_history_messages`` 过滤内部消息，格式为
        ``[RAW] N messages\\n{formatted}``，并记录 warning 日志。
        """
        from step124.runtime_context import public_history_messages

        limit = max_chars if max_chars is not None else _RAW_ARCHIVE_MAX_CHARS
        formatted = truncate_text(
            self._format_messages(public_history_messages(messages)),
            limit,
        )
        cursor = self.append_history(
            f"[RAW] {len(messages)} messages\n{formatted}",
            session_key=session_key,
        )
        logger.warning(
            "Memory consolidation degraded: raw-archived %d messages", len(messages)
        )
        return cursor

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
        """返回最新已分配的 cursor 值。

        对齐 nanobot：``_next_cursor`` 返回下一个可用 cursor，减 1 即最新已分配。
        ``max(..., 0)`` 确保无历史时返回 0。

        Returns:
            最新已分配的 cursor 值；无历史时返回 0。
        """
        return max(self._next_cursor() - 1, 0)

    @staticmethod
    def dream_session_key() -> str:
        """返回 Dream 运行的唯一 session key，如 ``dream:20260528-100000``。"""
        return f"dream:{datetime.now():%Y%m%d-%H%M%S}"

    @staticmethod
    def dream_run_completed(resp: object | None) -> bool:
        """仅当临时 Dream agent turn 干净完成时返回 True。

        检查 resp.metadata["_stop_reason"] == "completed"。
        """
        metadata = getattr(resp, "metadata", None)
        return isinstance(metadata, dict) and metadata.get("_stop_reason") == "completed"

    @staticmethod
    def build_dream_commit_message(prefix: str, diff_body: str) -> str:
        """基于真实工作区 diff 构建 Dream commit message。

        diff_body 是机器派生的文件变更摘要（见 dream_content_diff /
        GitStore.summarize_working_tree）。不包含 LLM 叙述，确保审计记录
        反映文件系统真相而非模型自报。

        空 diff_body 返回纯 prefix（auto_commit 在无内容时视为 no-op）。
        """
        diff_body = (diff_body or "").strip()
        if not diff_body:
            return prefix
        return f"{prefix}\n\n{diff_body}"

    @staticmethod
    def prune_dream_sessions(sessions_dir: Path, *, keep: int = 10) -> None:
        """清理最旧的 Dream session 文件，只保留最近 N 个。

        仅处理当前 base64url 编码的 Dream session key，非 dream session 文件
        不会被触碰。

        Args:
            sessions_dir: session 文件存储目录。
            keep: 保留的最近 dream session 数量，默认 10。
        """
        from step124.session import SessionManager

        dream_files = []
        for path in sessions_dir.glob("*.jsonl"):
            decoded_key = SessionManager._decode_storage_key(path.stem)
            if decoded_key is not None and decoded_key.startswith("dream:"):
                dream_files.append(path)
        dream_files.sort(key=lambda p: p.stat().st_mtime)
        if len(dream_files) <= keep:
            return

        to_remove = dream_files[: len(dream_files) - keep]
        for path in to_remove:
            try:
                path.unlink()
                logger.debug("Pruned old dream session: %s", path.stem)
            except OSError:
                pass

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
        template = self._dream_template()
        prompt = (
            f"{template}\n\n"
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

    # -- 数据校验层 ---------------------------------------------------------

    @staticmethod
    def _valid_cursor(value: Any) -> int | None:
        """校验 cursor 值：只接受非负 int，拒绝 bool。

        Python 中 ``isinstance(True, int)`` 为 True，必须显式拒绝 bool，
        否则 ``True`` 会被误判为 cursor=1。

        Args:
            value: 待校验的 cursor 值。

        Returns:
            合法的非负 int；不合法时返回 None。
        """
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        return value

    @staticmethod
    def _valid_history_payload(entry: dict[str, Any]) -> bool:
        """校验历史条目 payload 结构。

        必须包含 str 类型的 timestamp 和 content；session_key 可选但必须是 str。

        Args:
            entry: 待校验的条目字典。

        Returns:
            True 表示合法，False 表示畸形。
        """
        if not isinstance(entry.get("timestamp"), str):
            return False
        if not isinstance(entry.get("content"), str):
            return False
        session_key = entry.get("session_key")
        return session_key is None or isinstance(session_key, str)

    def _iter_valid_entries(self) -> Iterator[tuple[dict[str, Any], int]]:
        """遍历 history.jsonl，yield 有效条目 ``(entry, cursor)``。

        对每条记录做 cursor 和 payload 双重校验，无效条目跳过。
        首次遇到无效 cursor 或畸形 payload 时 ``logger.warning``，
        后续用 flag 限流，避免日志刷屏。

        Yields:
            ``(entry, valid_cursor)`` 元组。
        """
        poisoned: Any = None
        malformed_cursor: int | None = None
        for entry in self._read_entries():
            raw = entry.get("cursor")
            if raw is None:
                continue
            cursor = self._valid_cursor(raw)
            if cursor is None:
                poisoned = raw
                continue
            if not self._valid_history_payload(entry):
                malformed_cursor = cursor
                continue
            yield entry, cursor

        if poisoned is not None and not self._corruption_logged:
            self._corruption_logged = True
            logger.warning(
                "history.jsonl contains an invalid cursor (%r); dropping it. "
                "Usually caused by an external writer; further occurrences suppressed.",
                poisoned,
            )
        if malformed_cursor is not None and not self._malformed_entry_logged:
            self._malformed_entry_logged = True
            logger.warning(
                "history.jsonl contains a malformed entry at cursor %d; dropping it. "
                "Usually caused by an external writer; further occurrences suppressed.",
                malformed_cursor,
            )

    def _write_entries(self, entries: list[dict[str, Any]]) -> None:
        """原子覆盖写入 history.jsonl。

        先写入临时文件并 fsync 刷盘，再通过 ``os.replace`` 原子重命名，
        最后 fsync 父目录确保元数据落盘。进程在写入过程中崩溃时，
        history.jsonl 要么保持完整旧版本，要么变为完整新版本，
        不会出现半写损坏状态。

        Windows 上打开目录做 fsync 会抛 PermissionError，用 suppress 跳过——
        NTFS 本身会同步记录元数据日志。

        Args:
            entries: 要写入的条目列表。
        """
        tmp_path = self.history_file.with_suffix(self.history_file.suffix + ".tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                for entry in entries:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.history_file)

            # fsync 目录确保 rename 元数据落盘
            with suppress(PermissionError):
                fd = os.open(str(self.history_file.parent), os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

    def _next_cursor(self) -> int:
        """计算下一个可用 cursor 值。

        对齐 nanobot：优先用 cursor_counter（.cursor 文件）和最后一条 entry
        的 cursor 取 max+1；如果 last entry 无效则扫描全文件取 max。使用
        ``_iter_valid_entries`` 确保只统计有效 entry，避免外部写入的坏数据
        破坏 cursor 单调性。

        Returns:
            下一个可用的 cursor 值（>= 1）。
        """
        cursor_counter = self._read_cursor_counter()
        last = self._read_last_entry() or {}
        last_cursor = self._valid_cursor(last.get("cursor"))

        if cursor_counter is not None:
            if last_cursor is not None:
                return max(cursor_counter, last_cursor) + 1
            # cursor_counter 存在但 last 无效：扫描全文件取 max
            max_history_cursor = max((c for _, c in self._iter_valid_entries()), default=0)
            return max(cursor_counter, max_history_cursor) + 1

        # cursor_counter 不存在：last 有效则 last+1，否则扫描全文件
        if last_cursor is not None:
            return last_cursor + 1
        return max((c for _, c in self._iter_valid_entries()), default=0) + 1

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
        """格式化消息列表为单行文本，对齐 nanobot 格式。

        格式：``[timestamp] ROLE [tools: ...]: content``
        跳过无 content 的消息。
        """
        lines = []
        for message in messages:
            if not message.get("content"):
                continue
            tools = f" [tools: {', '.join(message['tools_used'])}]" if message.get("tools_used") else ""
            lines.append(
                f"[{message.get('timestamp', '?')[:16]}] {message['role'].upper()}{tools}: {message['content']}"
            )
        return "\n".join(lines)

    # -- Legacy HISTORY.md 迁移 ----------------------------------------------

    def migrate_legacy_history(self) -> int:
        """将旧版 HISTORY.md 迁移到 history.jsonl。

        迁移成功后备份原文件，返回迁移的条目数。无 HISTORY.md 或迁移失败
        返回 0。

        Returns:
            迁移的条目数；无文件或失败时返回 0。
        """
        if not self.legacy_history_file.exists():
            return 0
        try:
            text = self.legacy_history_file.read_text(encoding="utf-8")
            entries = self._parse_legacy_history(text)
            if not entries:
                return 0
            # 写入 history.jsonl
            self._write_entries(entries)
            last_cursor = entries[-1]["cursor"]
            self._cursor_file.write_text(str(last_cursor), encoding="utf-8")
            self._dream_cursor_file.write_text(str(last_cursor), encoding="utf-8")
            # 备份原文件
            backup_path = self._next_legacy_backup_path()
            self.legacy_history_file.replace(backup_path)
            logger.info("Migrated legacy HISTORY.md to history.jsonl (%d entries)", len(entries))
            return len(entries)
        except Exception:
            logger.exception("Failed to migrate legacy HISTORY.md")
            return 0

    def _parse_legacy_history(self, text: str) -> list[dict[str, Any]]:
        """解析旧版 HISTORY.md 文本为条目列表。"""
        normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not normalized:
            return []
        fallback_timestamp = self._legacy_fallback_timestamp()
        entries: list[dict[str, Any]] = []
        chunks = self._split_legacy_history_chunks(normalized)
        for cursor, chunk in enumerate(chunks, start=1):
            timestamp = fallback_timestamp
            content = chunk
            match = _LEGACY_TIMESTAMP_RE.match(chunk)
            if match:
                timestamp = match.group(1)
                remainder = chunk[match.end():].lstrip()
                if remainder:
                    content = remainder
            entries.append({"cursor": cursor, "timestamp": timestamp, "content": content})
        return entries

    def _split_legacy_history_chunks(self, text: str) -> list[str]:
        """将 HISTORY.md 文本拆分为条目块。"""
        lines = text.split("\n")
        chunks: list[str] = []
        current: list[str] = []
        saw_blank_separator = False
        for line in lines:
            if saw_blank_separator and line.strip() and current:
                chunks.append("\n".join(current).strip())
                current = [line]
                saw_blank_separator = False
                continue
            if self._should_start_new_legacy_chunk(line, current):
                chunks.append("\n".join(current).strip())
                current = [line]
                saw_blank_separator = False
                continue
            current.append(line)
            saw_blank_separator = not line.strip()
        if current:
            chunks.append("\n".join(current).strip())
        return [chunk for chunk in chunks if chunk]

    def _should_start_new_legacy_chunk(self, line: str, current: list[str]) -> bool:
        """判断当前行是否应开始新条目块。"""
        if not current:
            return False
        if not _LEGACY_ENTRY_START_RE.match(line):
            return False
        if self._is_raw_legacy_chunk(current) and _LEGACY_RAW_MESSAGE_RE.match(line):
            return False
        return True

    def _is_raw_legacy_chunk(self, lines: list[str]) -> bool:
        """判断当前块是否为 RAW 格式（含角色标记的消息）。"""
        first_nonempty = next((line for line in lines if line.strip()), "")
        match = _LEGACY_TIMESTAMP_RE.match(first_nonempty)
        if not match:
            return False
        return first_nonempty[match.end():].lstrip().startswith("[RAW]")

    def _legacy_fallback_timestamp(self) -> str:
        """返回旧文件 mtime 作为 fallback 时间戳。"""
        try:
            return datetime.fromtimestamp(
                self.legacy_history_file.stat().st_mtime,
            ).strftime("%Y-%m-%d %H:%M")
        except OSError:
            return datetime.now().strftime("%Y-%m-%d %H:%M")

    def _next_legacy_backup_path(self) -> Path:
        """返回下一个可用的备份文件路径。"""
        base = self.legacy_history_file.with_suffix(".md.bak")
        if not base.exists():
            return base
        i = 1
        while True:
            candidate = self.legacy_history_file.with_name(f"HISTORY.md.bak.{i}")
            if not candidate.exists():
                return candidate
            i += 1
