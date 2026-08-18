"""step33 Consolidation Replay Overflow 压缩测试（A16）。

覆盖：
- _replay_overflow_boundary 静态方法
- _consolidate_replay_overflow 方法
- maybe_consolidate_by_tokens 带 replay_max_messages
- estimate_session_prompt_tokens 方法

全构造数据：无真实 API，使用 mock provider。
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from step42.consolidation import Consolidator
from step42.llm import Runtime
from step42.session import Session, SessionManager


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _make_session(messages: list[dict], last_consolidated: int = 0) -> Session:
    return Session(
        key="test:1",
        messages=messages,
        last_consolidated=last_consolidated,
    )


def _make_mock_consolidator() -> tuple[Consolidator, MagicMock, MagicMock]:
    """构造一个 mock 的 Consolidator，返回 (consolidator, mock_store, mock_sessions)。"""
    mock_store = MagicMock()
    mock_sessions = MagicMock()
    mock_sessions.get_or_create = MagicMock(side_effect=lambda key: _make_session([]))
    mock_sessions.save = MagicMock()
    mock_sessions.invalidate = MagicMock()

    def _build_messages(**kwargs):
        return [{"role": "system", "content": "mock"}]

    def _get_tool_definitions():
        return []

    consolidator = Consolidator(
        store=mock_store,
        sessions=mock_sessions,
        build_messages=_build_messages,
        get_tool_definitions=_get_tool_definitions,
        consolidation_ratio=0.5,
        provider=None,
    )
    return consolidator, mock_store, mock_sessions


def _make_runtime(context_window_tokens: int = 128000, max_tokens: int = 4096) -> Runtime:
    runtime = MagicMock(spec=Runtime)
    runtime.context_window_tokens = context_window_tokens
    runtime.max_tokens = max_tokens
    runtime.model = "mock-model"
    runtime.provider = MagicMock()
    runtime.provider.chat = AsyncMock(return_value=MagicMock(content="summary"))
    return runtime


# ---------------------------------------------------------------------------
# _replay_overflow_boundary
# ---------------------------------------------------------------------------


class TestReplayOverflowBoundary:
    def test_no_replay_max_returns_none(self):
        session = _make_session([{"role": "user", "content": f"m{i}"} for i in range(10)])
        assert Consolidator._replay_overflow_boundary(session, None) is None

    def test_replay_max_zero_returns_none(self):
        session = _make_session([{"role": "user", "content": f"m{i}"} for i in range(10)])
        assert Consolidator._replay_overflow_boundary(session, 0) is None

    def test_tail_within_limit_returns_none(self):
        session = _make_session([{"role": "user", "content": f"m{i}"} for i in range(5)])
        assert Consolidator._replay_overflow_boundary(session, 10) is None

    def test_overflow_returns_user_aligned_index(self):
        # 10 条消息，replay_max=5，应该归档前 5 条
        messages = []
        for i in range(10):
            role = "user" if i % 2 == 0 else "assistant"
            messages.append({"role": role, "content": f"m{i}"})
        session = _make_session(messages)
        boundary = Consolidator._replay_overflow_boundary(session, 5)
        assert boundary is not None
        assert boundary > 0
        # boundary 应该对齐到 user 消息
        assert messages[boundary]["role"] == "user"

    def test_overflow_with_channel_delivery(self):
        messages = [
            {"role": "assistant", "content": "delivery", "_channel_delivery": True},
            {"role": "user", "content": "u0"},
            {"role": "assistant", "content": "a0"},
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "u2"},
            {"role": "assistant", "content": "a2"},
        ]
        session = _make_session(messages)
        # replay_max=3，recent_message_start_index 返回 4（7-3=4），
        # 从索引 4 开始找第一个 user：索引 4 是 a1，索引 5 是 u2，
        # 所以 boundary=5（对齐到 u2）
        boundary = Consolidator._replay_overflow_boundary(session, 3)
        assert boundary is not None
        assert boundary == 5
        assert messages[boundary]["role"] == "user"

    def test_legal_start_drops_orphan_tools(self):
        messages = [
            {"role": "user", "content": "u0"},
            {"role": "assistant", "content": "a0"},
            {"role": "tool", "content": "orphan", "tool_call_id": "nonexistent"},
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "u2"},
            {"role": "assistant", "content": "a2"},
        ]
        session = _make_session(messages)
        boundary = Consolidator._replay_overflow_boundary(session, 3)
        assert boundary is not None
        # boundary 不应该指向孤立的 tool 结果
        assert messages[boundary]["role"] != "tool"

    def test_boundary_before_last_consolidated_returns_none(self):
        messages = [{"role": "user", "content": f"m{i}"} for i in range(10)]
        session = _make_session(messages, last_consolidated=8)
        # 只有 2 条未归档，replay_max=5，不溢出
        assert Consolidator._replay_overflow_boundary(session, 5) is None


# ---------------------------------------------------------------------------
# _consolidate_replay_overflow
# ---------------------------------------------------------------------------


class TestConsolidateReplayOverflow:
    @pytest.mark.asyncio
    async def test_consolidates_overflow_chunk(self):
        consolidator, mock_store, mock_sessions = _make_mock_consolidator()
        runtime = _make_runtime()

        messages = []
        for i in range(10):
            role = "user" if i % 2 == 0 else "assistant"
            messages.append({"role": role, "content": f"m{i}"})
        session = _make_session(messages)

        # mock archive 方法
        consolidator.archive = AsyncMock(return_value="test summary")

        summary = await consolidator._consolidate_replay_overflow(
            session, 5, runtime=runtime,
        )

        assert summary == "test summary"
        assert session.last_consolidated > 0
        consolidator.archive.assert_called_once()
        mock_sessions.save.assert_called()

    @pytest.mark.asyncio
    async def test_no_overflow_returns_none(self):
        consolidator, mock_store, mock_sessions = _make_mock_consolidator()
        runtime = _make_runtime()

        messages = [{"role": "user", "content": f"m{i}"} for i in range(3)]
        session = _make_session(messages)

        consolidator.archive = AsyncMock()

        summary = await consolidator._consolidate_replay_overflow(
            session, 10, runtime=runtime,
        )

        assert summary is None
        consolidator.archive.assert_not_called()

    @pytest.mark.asyncio
    async def test_updates_last_consolidated(self):
        consolidator, mock_store, mock_sessions = _make_mock_consolidator()
        runtime = _make_runtime()

        messages = []
        for i in range(10):
            role = "user" if i % 2 == 0 else "assistant"
            messages.append({"role": role, "content": f"m{i}"})
        session = _make_session(messages, last_consolidated=0)

        consolidator.archive = AsyncMock(return_value="summary")

        await consolidator._consolidate_replay_overflow(
            session, 5, runtime=runtime,
        )

        # last_consolidated 应该被更新
        assert session.last_consolidated > 0
        assert session.last_consolidated <= len(messages)


# ---------------------------------------------------------------------------
# maybe_consolidate_by_tokens 带 replay_max_messages
# ---------------------------------------------------------------------------


class TestMaybeConsolidateWithReplayMax:
    @pytest.mark.asyncio
    async def test_calls_replay_overflow_first(self):
        consolidator, mock_store, mock_sessions = _make_mock_consolidator()
        runtime = _make_runtime()

        messages = []
        for i in range(20):
            role = "user" if i % 2 == 0 else "assistant"
            messages.append({"role": role, "content": f"m{i}" * 100})  # 大内容确保 token 超预算
        session = _make_session(messages)

        # mock_sessions.get_or_create 返回同一个 session
        mock_sessions.get_or_create = MagicMock(return_value=session)

        # mock _consolidate_replay_overflow
        consolidator._consolidate_replay_overflow = AsyncMock(return_value="replay_summary")
        consolidator.archive = AsyncMock(return_value="token_summary")

        await consolidator.maybe_consolidate_by_tokens(
            session, runtime=runtime, replay_max_messages=5,
        )

        # replay overflow 应该被调用
        consolidator._consolidate_replay_overflow.assert_called_once_with(
            session, 5, runtime=runtime,
        )

    @pytest.mark.asyncio
    async def test_replay_max_none_skips_replay_overflow(self):
        consolidator, mock_store, mock_sessions = _make_mock_consolidator()
        runtime = _make_runtime()

        messages = [{"role": "user", "content": "small"}]
        session = _make_session(messages)
        mock_sessions.get_or_create = MagicMock(return_value=session)

        # _consolidate_replay_overflow 会被调用，但内部 _replay_overflow_boundary 返回 None
        # 所以 archive 不会被调用
        consolidator.archive = AsyncMock()

        await consolidator.maybe_consolidate_by_tokens(
            session, runtime=runtime, replay_max_messages=None,
        )

        consolidator.archive.assert_not_called()


# ---------------------------------------------------------------------------
# estimate_session_prompt_tokens
# ---------------------------------------------------------------------------


class TestEstimateSessionPromptTokens:
    def test_returns_tuple(self):
        consolidator, mock_store, mock_sessions = _make_mock_consolidator()
        runtime = _make_runtime()

        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        session = _make_session(messages)

        result = consolidator.estimate_session_prompt_tokens(session, runtime=runtime)

        assert isinstance(result, tuple)
        assert len(result) == 2
        tokens, source = result
        assert isinstance(tokens, int)
        assert tokens > 0
        assert isinstance(source, str)

    def test_includes_last_summary(self):
        consolidator, mock_store, mock_sessions = _make_mock_consolidator()
        runtime = _make_runtime()

        messages = [{"role": "user", "content": "hello"}]
        session = _make_session(messages)
        session.metadata["_last_summary"] = {"text": "previous summary", "last_active": "2026-01-01"}

        result = consolidator.estimate_session_prompt_tokens(session, runtime=runtime)

        assert result[0] > 0
