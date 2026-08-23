"""step125: _format_messages + raw_archive 格式对齐测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from step125.memory import MemoryStore


@pytest.fixture
def store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(workspace=str(tmp_path))


class TestFormatMessages:
    """_format_messages 格式测试。"""

    def test_basic_format(self) -> None:
        """基本格式：[timestamp] ROLE: content。"""
        messages = [
            {"role": "user", "content": "hello", "timestamp": "2026-01-01 10:00:00"},
        ]
        result = MemoryStore._format_messages(messages)
        assert "[2026-01-01 10:00]" in result
        assert "USER" in result
        assert "hello" in result

    def test_skips_empty_content(self) -> None:
        """跳过无 content 的消息。"""
        messages = [
            {"role": "user", "content": ""},
            {"role": "assistant", "content": "hi"},
        ]
        result = MemoryStore._format_messages(messages)
        assert "hi" in result
        assert result.count("\n") == 0  # 只有一条

    def test_with_tools_used(self) -> None:
        """包含 tools_used 时显示工具列表。"""
        messages = [
            {"role": "assistant", "content": "done", "tools_used": ["read_file", "write_file"]},
        ]
        result = MemoryStore._format_messages(messages)
        assert "[tools: read_file, write_file]" in result

    def test_multiple_messages(self) -> None:
        """多条消息用换行分隔。"""
        messages = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "second"},
        ]
        result = MemoryStore._format_messages(messages)
        lines = result.split("\n")
        assert len(lines) == 2

    def test_missing_timestamp_uses_question_mark(self) -> None:
        """缺失 timestamp 时用 ?。"""
        messages = [{"role": "user", "content": "test"}]
        result = MemoryStore._format_messages(messages)
        assert "[?]" in result


class TestRawArchive:
    """raw_archive 格式测试。"""

    def test_includes_message_count(self, store: MemoryStore) -> None:
        """包含消息计数。"""
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        store.raw_archive(messages)
        entries = store._read_entries()
        assert len(entries) == 1
        assert "[RAW] 2 messages" in entries[0]["content"]

    def test_includes_formatted_messages(self, store: MemoryStore) -> None:
        """包含格式化后的消息内容。"""
        messages = [
            {"role": "user", "content": "hello world"},
        ]
        store.raw_archive(messages)
        entries = store._read_entries()
        assert "hello world" in entries[0]["content"]

    def test_respects_max_chars(self, store: MemoryStore) -> None:
        """尊重 max_chars 限制。"""
        messages = [
            {"role": "user", "content": "a" * 1000},
        ]
        store.raw_archive(messages, max_chars=100)
        entries = store._read_entries()
        assert len(entries[0]["content"]) < 200  # 包含 [RAW] 前缀和截断后缀

    def test_empty_messages(self, store: MemoryStore) -> None:
        """空消息列表也能处理。"""
        store.raw_archive([])
        entries = store._read_entries()
        assert len(entries) == 1
        assert "[RAW] 0 messages" in entries[0]["content"]

    def test_returns_cursor(self, store: MemoryStore) -> None:
        """返回 cursor 值。"""
        messages = [{"role": "user", "content": "test"}]
        cursor = store.raw_archive(messages)
        assert cursor == 1
