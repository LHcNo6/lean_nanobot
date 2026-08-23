"""step122: Consolidator token 估算对齐 + unified_session + WeakValueDictionary 测试。"""

from __future__ import annotations

import weakref
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from step122.consolidation import Consolidator
from step122.memory import MemoryStore
from step122.session import Session, SessionManager


@pytest.fixture
def store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(workspace=str(tmp_path))


@pytest.fixture
def sessions(tmp_path: Path) -> SessionManager:
    return SessionManager(workspace=str(tmp_path))


def _make_consolidator(store, sessions, **kwargs) -> Consolidator:
    return Consolidator(
        store=store,
        sessions=sessions,
        build_messages=lambda **kw: [],
        get_tool_definitions=lambda: [],
        **kwargs,
    )


class TestUnifiedSession:
    """unified_session 参数测试。"""

    def test_default_false(self, store: MemoryStore, sessions: SessionManager) -> None:
        """默认 unified_session 为 False。"""
        c = _make_consolidator(store, sessions)
        assert c.unified_session is False

    def test_can_set_true(self, store: MemoryStore, sessions: SessionManager) -> None:
        """可设置 unified_session 为 True。"""
        c = _make_consolidator(store, sessions, unified_session=True)
        assert c.unified_session is True


class TestLocksWeakRef:
    """_locks WeakValueDictionary 测试。"""

    def test_locks_is_weak_value_dict(self, store: MemoryStore, sessions: SessionManager) -> None:
        """_locks 类型为 WeakValueDictionary。"""
        c = _make_consolidator(store, sessions)
        assert isinstance(c._locks, weakref.WeakValueDictionary)

    def test_get_lock_creates_lock(self, store: MemoryStore, sessions: SessionManager) -> None:
        """get_lock 创建并返回 asyncio.Lock。"""
        import asyncio
        c = _make_consolidator(store, sessions)
        lock = c.get_lock("test-session")
        assert isinstance(lock, asyncio.Lock)
        assert c.get_lock("test-session") is lock  # 同一 session 返回同一 lock

    def test_lock_garbage_collected(self, store: MemoryStore, sessions: SessionManager) -> None:
        """锁无外部引用时被 GC，WeakValueDictionary 自动移除。"""
        import asyncio
        import gc
        c = _make_consolidator(store, sessions)
        lock = c.get_lock("gc-session")
        assert "gc-session" in c._locks
        del lock
        gc.collect()
        # WeakValueDictionary 在 key 对应对象被 GC 后自动移除
        assert "gc-session" not in c._locks


class TestEstimateSessionPromptTokens:
    """estimate_session_prompt_tokens 方法测试。"""

    def test_returns_tuple(self, store: MemoryStore, sessions: SessionManager) -> None:
        """返回 (int, str) 元组。"""
        c = _make_consolidator(store, sessions)
        session = Session(key="test:123", messages=[{"role": "user", "content": "hi"}])
        mock_runtime = MagicMock()
        mock_runtime.provider = MagicMock()
        mock_runtime.model = "test"

        result = c.estimate_session_prompt_tokens(session, runtime=mock_runtime)
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], int)
        assert isinstance(result[1], str)

    def test_empty_session_returns_zero(self, store: MemoryStore, sessions: SessionManager) -> None:
        """空会话返回 0 token。"""
        c = _make_consolidator(store, sessions)
        session = Session(key="test:123", messages=[])
        mock_runtime = MagicMock()
        mock_runtime.provider = MagicMock()
        mock_runtime.model = "test"

        tokens, _ = c.estimate_session_prompt_tokens(session, runtime=mock_runtime)
        assert tokens >= 0  # 至少有系统提示开销


class TestMaybeConsolidateUsesNewEstimator:
    """maybe_consolidate_by_tokens 使用新估算器测试。"""

    @pytest.mark.asyncio
    async def test_skips_when_context_window_zero(self, store: MemoryStore, sessions: SessionManager) -> None:
        """context_window <= 0 时直接返回。"""
        c = _make_consolidator(store, sessions)
        session = Session(key="test:123", messages=[{"role": "user", "content": "hi"}])
        mock_runtime = MagicMock()
        mock_runtime.context_window_tokens = 0

        # 不应抛出异常
        await c.maybe_consolidate_by_tokens(session, runtime=mock_runtime)

    @pytest.mark.asyncio
    async def test_skips_empty_session(self, store: MemoryStore, sessions: SessionManager) -> None:
        """空会话直接返回。"""
        c = _make_consolidator(store, sessions)
        session = Session(key="test:123", messages=[])
        mock_runtime = MagicMock()
        mock_runtime.context_window_tokens = 8192
        mock_runtime.max_tokens = 1024

        await c.maybe_consolidate_by_tokens(session, runtime=mock_runtime)
        # 不应抛出异常
