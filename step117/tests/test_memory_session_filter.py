"""step117: 内部会话过滤 + 近期历史注入测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from step117.context import ContextBuilder
from step117.memory import MemoryStore


@pytest.fixture
def store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(workspace=str(tmp_path))


def _write_raw_entries(store: MemoryStore, entries: list[dict]) -> None:
    with open(store.history_file, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


class TestIsInternalSession:
    """_is_internal_history_session 测试。"""

    def test_cron_prefix(self) -> None:
        assert MemoryStore._is_internal_history_session("cron:daily") is True

    def test_dream_prefix(self) -> None:
        assert MemoryStore._is_internal_history_session("dream:20260101") is True

    def test_heartbeat_exact(self) -> None:
        assert MemoryStore._is_internal_history_session("heartbeat") is True

    def test_normal_session(self) -> None:
        assert MemoryStore._is_internal_history_session("cli:123") is False

    def test_none(self) -> None:
        assert MemoryStore._is_internal_history_session(None) is False

    def test_empty_string(self) -> None:
        assert MemoryStore._is_internal_history_session("") is False


class TestReadRecentHistoryForPrompt:
    """read_recent_history_for_prompt 测试。"""

    def test_no_session_key_returns_all(self, store: MemoryStore) -> None:
        _write_raw_entries(store, [
            {"cursor": 1, "timestamp": "t1", "content": "c1", "session_key": "cli:1"},
            {"cursor": 2, "timestamp": "t2", "content": "c2", "session_key": "dream:x"},
            {"cursor": 3, "timestamp": "t3", "content": "c3"},
        ])
        result = store.read_recent_history_for_prompt(0, session_key=None)
        assert len(result) == 3

    def test_session_filter_exact(self, store: MemoryStore) -> None:
        _write_raw_entries(store, [
            {"cursor": 1, "timestamp": "t1", "content": "c1", "session_key": "cli:1"},
            {"cursor": 2, "timestamp": "t2", "content": "c2", "session_key": "cli:2"},
            {"cursor": 3, "timestamp": "t3", "content": "c3", "session_key": "dream:x"},
        ])
        result = store.read_recent_history_for_prompt(0, session_key="cli:1", unified_session=False)
        assert len(result) == 1
        assert result[0]["content"] == "c1"

    def test_unified_session_includes_non_internal(self, store: MemoryStore) -> None:
        _write_raw_entries(store, [
            {"cursor": 1, "timestamp": "t1", "content": "c1", "session_key": "cli:1"},
            {"cursor": 2, "timestamp": "t2", "content": "c2", "session_key": "cli:2"},
            {"cursor": 3, "timestamp": "t3", "content": "c3", "session_key": "dream:x"},
            {"cursor": 4, "timestamp": "t4", "content": "c4"},
        ])
        result = store.read_recent_history_for_prompt(0, session_key="cli:1", unified_session=True)
        # cli:1 + cli:2 + no session_key = 3（dream:x 被排除）
        assert len(result) == 3
        contents = [e["content"] for e in result]
        assert "c1" in contents
        assert "c2" in contents
        assert "c4" in contents
        assert "c3" not in contents

    def test_since_cursor_filter(self, store: MemoryStore) -> None:
        _write_raw_entries(store, [
            {"cursor": 1, "timestamp": "t1", "content": "c1", "session_key": "cli:1"},
            {"cursor": 2, "timestamp": "t2", "content": "c2", "session_key": "cli:1"},
            {"cursor": 3, "timestamp": "t3", "content": "c3", "session_key": "cli:1"},
        ])
        result = store.read_recent_history_for_prompt(1, session_key="cli:1")
        assert len(result) == 2
        assert result[0]["cursor"] == 2


class TestContextRecentHistoryInjection:
    """context.py 近期历史注入测试。"""

    def test_recent_history_injected(self, tmp_path: Path) -> None:
        builder = ContextBuilder(workspace=str(tmp_path))
        builder.memory.append_history("recent message", session_key="cli:1")
        prompt = builder.build_system_prompt(session_key="cli:1")
        assert "# Recent History" in prompt
        assert "recent message" in prompt

    def test_no_recent_history_when_empty(self, tmp_path: Path) -> None:
        builder = ContextBuilder(workspace=str(tmp_path))
        prompt = builder.build_system_prompt(session_key="cli:1")
        assert "# Recent History" not in prompt

    def test_exclude_memory_flag(self, tmp_path: Path) -> None:
        builder = ContextBuilder(workspace=str(tmp_path))
        builder.memory.append_history("recent message", session_key="cli:1")
        prompt = builder.build_system_prompt(include_memory_recent_history=False, session_key="cli:1")
        assert "# Recent History" not in prompt

    def test_internal_session_not_leaked(self, tmp_path: Path) -> None:
        builder = ContextBuilder(workspace=str(tmp_path))
        builder.memory.append_history("dream internal", session_key="dream:x")
        builder.memory.append_history("user message", session_key="cli:1")
        prompt = builder.build_system_prompt(session_key="cli:1", unified_session=True)
        assert "dream internal" not in prompt
        assert "user message" in prompt
