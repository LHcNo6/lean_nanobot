"""step29 会话域包（对齐 nanobot ``session/`` 布局）。

step27 起 ``Session`` / ``SessionManager`` 位于单文件 ``session.py``，
step29 将其升级为包：类定义移到 ``manager.py``，新增 ``keys.py`` /
``history_visibility.py`` / ``turn_continuation.py`` 三个策略模块。
此处 re-export 保持 ``from step41.session import Session, SessionManager``
兼容（既有调用方零改动）。
"""

from __future__ import annotations

from step41.session.manager import (
    FILE_MAX_MESSAGES,
    SESSION_CACHE_MAX_SIZE,
    RetentionResult,
    Session,
    SessionManager,
    ensure_dir,
    safe_filename,
)

__all__ = [
    "FILE_MAX_MESSAGES",
    "SESSION_CACHE_MAX_SIZE",
    "RetentionResult",
    "Session",
    "SessionManager",
    "ensure_dir",
    "safe_filename",
]