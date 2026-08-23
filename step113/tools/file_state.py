"""文件读写状态追踪（step39，对齐 nanobot tools/file_state.py）。

用途：
- read-before-edit 警告：编辑文件前检查是否已读取，避免覆盖未查看的内容；
- read deduplication：文件内容未变时跳过重复读取，节省 token。

通过 ContextVar 绑定到当前 async task，工具内通过 ``current_file_states()`` 查询。
"""

from __future__ import annotations

import hashlib
import os
from contextvars import ContextVar, Token
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class ReadState:
    """单次文件读取的状态记录。"""

    mtime: float
    offset: int
    limit: int | None
    content_hash: str | None
    can_dedup: bool


def _hash_file(p: str) -> str | None:
    """计算文件 SHA-256 哈希，文件不可读时返回 None。"""
    try:
        return hashlib.sha256(Path(p).read_bytes()).hexdigest()
    except OSError:
        return None


class FileStates:
    """单会话文件读写追踪器。

    拥有独立的状态字典，使 read-dedup 和 read-before-edit 警告限定在
    单个 agent 会话内，不会跨会话泄漏。
    """

    __slots__ = ("_state",)

    def __init__(self) -> None:
        self._state: dict[str, ReadState] = {}

    def record_read(
        self, path: str | Path, offset: int = 1, limit: int | None = None
    ) -> None:
        """记录文件已读取（读取成功后调用）。

        Args:
            path: 文件路径。
            offset: 读取起始行（从 1 开始）。
            limit: 读取行数上限，None 表示全部。
        """
        p = str(Path(path).resolve())
        try:
            mtime = os.path.getmtime(p)
        except OSError:
            return
        self._state[p] = ReadState(
            mtime=mtime,
            offset=offset,
            limit=limit,
            content_hash=_hash_file(p),
            can_dedup=True,
        )

    def record_write(self, path: str | Path) -> None:
        """记录文件已写入（更新 mtime，标记不可 dedup）。

        Args:
            path: 文件路径。
        """
        p = str(Path(path).resolve())
        try:
            mtime = os.path.getmtime(p)
        except OSError:
            self._state.pop(p, None)
            return
        self._state[p] = ReadState(
            mtime=mtime,
            offset=1,
            limit=None,
            content_hash=_hash_file(p),
            can_dedup=False,
        )

    def check_read(self, path: str | Path) -> str | None:
        """检查文件是否已读取且未被修改。

        Returns:
            None 表示可以安全编辑；否则返回警告字符串。
            mtime 变化但内容哈希相同（如 touch、编辑器保存）时视为未修改，
            避免误报。
        """
        p = str(Path(path).resolve())
        entry = self._state.get(p)
        if entry is None:
            return (
                "Warning: file has not been read yet. "
                "Read it first to verify content before editing."
            )
        try:
            current_mtime = os.path.getmtime(p)
        except OSError:
            return None
        if current_mtime != entry.mtime:
            if entry.content_hash and _hash_file(p) == entry.content_hash:
                entry.mtime = current_mtime
                return None
            return (
                "Warning: file has been modified since last read. "
                "Re-read to verify content before editing."
            )
        # mtime 未变——仍检查内容哈希以检测快速修改
        if entry.content_hash and _hash_file(p) != entry.content_hash:
            return (
                "Warning: file has been modified since last read. "
                "Re-read to verify content before editing."
            )
        return None

    def is_unchanged(
        self, path: str | Path, offset: int = 1, limit: int | None = None
    ) -> bool:
        """判断文件是否之前以相同参数读取过且内容未变（用于 read dedup）。

        Args:
            path: 文件路径。
            offset: 读取起始行。
            limit: 读取行数上限。

        Returns:
            True 表示可以跳过重复读取；False 表示需要完整读取。
        """
        p = str(Path(path).resolve())
        entry = self._state.get(p)
        if entry is None:
            return False
        if not entry.can_dedup:
            return False
        if entry.offset != offset or entry.limit != limit:
            return False
        try:
            current_mtime = os.path.getmtime(p)
        except OSError:
            return False
        if current_mtime != entry.mtime:
            # mtime 变化——检查内容是否也变化
            current_hash = _hash_file(p)
            if current_hash != entry.content_hash:
                # 内容实际变化——不 dedup
                entry.can_dedup = False
                return False
            # 内容相同但 mtime 变化（如 touch）——标记为不可 dedup 以强制下次完整读取
            entry.can_dedup = False
            return True
        # mtime 未变——内容必然相同
        return True

    def get(self, path: str | Path) -> ReadState | None:
        """返回文件的原始 ReadState 记录，无则返回 None。"""
        return self._state.get(str(Path(path).resolve()))

    def clear(self) -> None:
        """清空所有追踪状态（测试用）。"""
        self._state.clear()


class FileStateStore:
    """按 session_key 存储 FileStates 的查找表。"""

    __slots__ = ("_states_by_key",)

    def __init__(self) -> None:
        self._states_by_key: dict[str, FileStates] = {}

    def for_session(self, session_key: str | None) -> FileStates:
        """获取/创建指定会话的 FileStates。

        Args:
            session_key: 会话 key，None 时使用 "__default__"。

        Returns:
            该会话对应的 FileStates 实例。
        """
        key = session_key or "__default__"
        states = self._states_by_key.get(key)
        if states is None:
            states = FileStates()
            self._states_by_key[key] = states
        return states

    def clear(self) -> None:
        """清空所有会话状态（测试用）。"""
        self._states_by_key.clear()


# ---------------------------------------------------------------------------
# ContextVar 绑定
# ---------------------------------------------------------------------------

_current_file_states: ContextVar[FileStates | None] = ContextVar(
    "lean_nanobot_file_states",
    default=None,
)


def current_file_states(default: FileStates) -> FileStates:
    """返回当前 agent task 绑定的 FileStates，无则返回 default。

    Args:
        default: 无绑定时的回退实例。

    Returns:
        当前绑定的 FileStates 或 default。
    """
    return _current_file_states.get() or default


def bind_file_states(file_states: FileStates) -> Token[FileStates | None]:
    """为当前 async task 绑定文件读写状态。

    Args:
        file_states: 要绑定的 FileStates 实例。

    Returns:
        ContextVar token，用于 reset_file_states 恢复。
    """
    return _current_file_states.set(file_states)


def reset_file_states(token: Token[FileStates | None]) -> None:
    """恢复上一次绑定的 FileStates。

    Args:
        token: bind_file_states 返回的 token。
    """
    _current_file_states.reset(token)
