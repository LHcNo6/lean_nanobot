"""step107: Consolidator.archive 系统提示 + LLM 调用对齐测试。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from step107.consolidation import Consolidator, _CONSOLIDATOR_SYSTEM_PROMPT
from step107.memory import MemoryStore
from step107.session import SessionManager


@pytest.fixture
def store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(workspace=str(tmp_path))


@pytest.fixture
def sessions(tmp_path: Path) -> SessionManager:
    return SessionManager(workspace=str(tmp_path))


@pytest.fixture
def consolidator(store: MemoryStore, sessions: SessionManager) -> Consolidator:
    return Consolidator(
        store=store,
        sessions=sessions,
        build_messages=lambda **kw: [],
        get_tool_definitions=lambda: [],
    )


class TestSystemPrompt:
    """系统提示模板测试。"""

    def test_contains_snip_criteria(self) -> None:
        """系统提示包含 SNIP 分类标准。"""
        assert "Signal" in _CONSOLIDATOR_SYSTEM_PROMPT
        assert "Novel" in _CONSOLIDATOR_SYSTEM_PROMPT
        assert "Important" in _CONSOLIDATOR_SYSTEM_PROMPT
        assert "Persistent" in _CONSOLIDATOR_SYSTEM_PROMPT

    def test_contains_marks(self) -> None:
        """系统提示包含所有 mark 类型。"""
        assert "[permanent]" in _CONSOLIDATOR_SYSTEM_PROMPT
        assert "[durable]" in _CONSOLIDATOR_SYSTEM_PROMPT
        assert "[ephemeral]" in _CONSOLIDATOR_SYSTEM_PROMPT
        assert "[correction]" in _CONSOLIDATOR_SYSTEM_PROMPT
        assert "[skip]" in _CONSOLIDATOR_SYSTEM_PROMPT

    def test_contains_nothing_output(self) -> None:
        """系统提示包含 (nothing) 输出约定。"""
        assert "(nothing)" in _CONSOLIDATOR_SYSTEM_PROMPT


def _make_runtime(model="test-model", max_tokens=1024, temperature=0.7, context_window=8192):
    """创建带完整属性的 mock runtime。"""
    mock_runtime = MagicMock()
    mock_runtime.model = model
    mock_runtime.max_tokens = max_tokens
    mock_runtime.temperature = temperature
    mock_runtime.context_window_tokens = context_window
    return mock_runtime


class TestArchiveLLMCall:
    """archive 方法 LLM 调用测试。"""

    @pytest.mark.asyncio
    async def test_passes_temperature(self, consolidator: Consolidator) -> None:
        """LLM 调用传递 temperature 参数。"""
        mock_runtime = _make_runtime(temperature=0.3)
        mock_runtime.provider.chat = AsyncMock(return_value=MagicMock(
            content="[durable] test fact", finish_reason="stop",
        ))

        await consolidator.archive(
            [{"role": "user", "content": "hello"}],
            runtime=mock_runtime,
        )

        call_kwargs = mock_runtime.provider.chat.call_args.kwargs
        assert call_kwargs["temperature"] == 0.3

    @pytest.mark.asyncio
    async def test_uses_runtime_max_tokens(self, consolidator: Consolidator) -> None:
        """max_tokens 使用 runtime.max_tokens 而非硬编码。"""
        mock_runtime = _make_runtime(max_tokens=4096)
        mock_runtime.provider.chat = AsyncMock(return_value=MagicMock(
            content="summary", finish_reason="stop",
        ))

        await consolidator.archive(
            [{"role": "user", "content": "hello"}],
            runtime=mock_runtime,
        )

        call_kwargs = mock_runtime.provider.chat.call_args.kwargs
        assert call_kwargs["max_tokens"] == 4096

    @pytest.mark.asyncio
    async def test_finish_reason_error_triggers_fallback(self, consolidator: Consolidator, store: MemoryStore) -> None:
        """finish_reason == error 时触发 raw_archive 回退。"""
        mock_runtime = _make_runtime()
        mock_runtime.provider.chat = AsyncMock(return_value=MagicMock(
            content="", finish_reason="error",
        ))

        result = await consolidator.archive(
            [{"role": "user", "content": "hello"}],
            runtime=mock_runtime,
        )

        assert result is None  # 回退返回 None
        # raw_archive 应该写入了历史
        entries = store._read_entries()
        assert len(entries) >= 1

    @pytest.mark.asyncio
    async def test_tools_none_passed(self, consolidator: Consolidator) -> None:
        """LLM 调用传递 tools=None 和 tool_choice=None。"""
        mock_runtime = _make_runtime()
        mock_runtime.provider.chat = AsyncMock(return_value=MagicMock(
            content="summary", finish_reason="stop",
        ))

        await consolidator.archive(
            [{"role": "user", "content": "hello"}],
            runtime=mock_runtime,
        )

        call_kwargs = mock_runtime.provider.chat.call_args.kwargs
        assert call_kwargs.get("tools") is None
        assert call_kwargs.get("tool_choice") is None

    @pytest.mark.asyncio
    async def test_empty_messages_returns_none(self, consolidator: Consolidator) -> None:
        """空消息列表返回 None，不调用 LLM。"""
        mock_runtime = MagicMock()
        result = await consolidator.archive([], runtime=mock_runtime)
        assert result is None
        mock_runtime.provider.chat.assert_not_called()

    @pytest.mark.asyncio
    async def test_successful_archive_returns_summary(self, consolidator: Consolidator) -> None:
        """成功归档返回摘要文本。"""
        mock_runtime = _make_runtime()
        mock_runtime.provider.chat = AsyncMock(return_value=MagicMock(
            content="[durable] key fact", finish_reason="stop",
        ))

        result = await consolidator.archive(
            [{"role": "user", "content": "hello"}],
            runtime=mock_runtime,
        )

        assert result == "[durable] key fact"
