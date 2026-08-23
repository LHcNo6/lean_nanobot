"""step115: append_history 安全增强测试。"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from step115.memory import MemoryStore


@pytest.fixture
def store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(workspace=str(tmp_path))


class TestStripThinkIntegration:
    """append_history 中 strip_think 集成测试。"""

    def test_normal_content_unchanged(self, store: MemoryStore) -> None:
        """普通内容不受 strip_think 影响。"""
        cursor = store.append_history("hello world")
        entries = store._read_entries()
        assert entries[0]["content"] == "hello world"
        assert cursor == 1

    def test_think_block_removed(self, store: MemoryStore) -> None:
        """<think>...</think> 块被移除。"""
        store.append_history("<think>internal reasoning</think>actual answer")
        entries = store._read_entries()
        assert "internal reasoning" not in entries[0]["content"]
        assert "actual answer" in entries[0]["content"]

    def test_unclosed_think_removed(self, store: MemoryStore) -> None:
        """未闭合的 <think> 前缀被移除。"""
        store.append_history("<think>some reasoning that never closes")
        entries = store._read_entries()
        assert entries[0]["content"] == ""

    def test_strip_to_empty_persists_empty(self, store: MemoryStore) -> None:
        """raw 非空但 strip 后为空时，持久化空串（不回退 raw）。"""
        store.append_history("<think>leak</think>")
        entries = store._read_entries()
        assert entries[0]["content"] == ""
        # 确认不是 raw 内容
        assert "leak" not in entries[0]["content"]

    def test_unicode_content_preserved(self, store: MemoryStore) -> None:
        """中文等 Unicode 内容保留。"""
        store.append_history("用户说：你好")
        entries = store._read_entries()
        assert entries[0]["content"] == "用户说：你好"


class TestOversizeWarning:
    """超限条目警告限流测试。"""

    def test_oversize_truncated(self, store: MemoryStore) -> None:
        """超过默认 cap 的内容被截断（truncate_text 会加 15 字符后缀）。"""
        big = "x" * 65000
        store.append_history(big)
        entries = store._read_entries()
        # truncate_text 返回 text[:limit] + "... (truncated)"（15 字符后缀）
        assert len(entries[0]["content"]) <= 64000 + 15
        assert entries[0]["content"].endswith("... (truncated)")

    def test_oversize_custom_cap(self, store: MemoryStore) -> None:
        """自定义 max_chars 生效。"""
        store.append_history("a" * 200, max_chars=50)
        entries = store._read_entries()
        assert len(entries[0]["content"]) <= 50 + 15  # +后缀
        assert entries[0]["content"].endswith("... (truncated)")

    def test_oversize_warning_only_once(self, store: MemoryStore, caplog: pytest.LogCaptureFixture) -> None:
        """超限警告只输出一次。"""
        with caplog.at_level(logging.WARNING):
            store.append_history("x" * 70000)
            store.append_history("y" * 70000)
            store.append_history("z" * 70000)
        warning_count = sum(
            1 for r in caplog.records
            if "exceeds" in r.message and "truncating" in r.message
        )
        assert warning_count == 1, f"超限警告应只出现一次，实际 {warning_count} 次"

    def test_no_warning_for_normal_size(self, store: MemoryStore, caplog: pytest.LogCaptureFixture) -> None:
        """正常大小不触发超限警告。"""
        with caplog.at_level(logging.WARNING):
            store.append_history("normal content")
        assert not any("exceeds" in r.message for r in caplog.records)


class TestAppendHistoryGeneral:
    """append_history 通用行为测试。"""

    def test_returns_cursor(self, store: MemoryStore) -> None:
        """返回自增 cursor。"""
        assert store.append_history("first") == 1
        assert store.append_history("second") == 2
        assert store.append_history("third") == 3

    def test_session_key_recorded(self, store: MemoryStore) -> None:
        """session_key 被记录。"""
        store.append_history("msg", session_key="cli:123")
        entries = store._read_entries()
        assert entries[0]["session_key"] == "cli:123"

    def test_no_session_key_field_when_none(self, store: MemoryStore) -> None:
        """session_key 为 None 时不记录该字段。"""
        store.append_history("msg")
        entries = store._read_entries()
        assert "session_key" not in entries[0]

    def test_timestamp_format(self, store: MemoryStore) -> None:
        """timestamp 格式为 YYYY-MM-DD HH:MM。"""
        store.append_history("msg")
        entries = store._read_entries()
        ts = entries[0]["timestamp"]
        assert len(ts) == 16
        assert ts[4] == "-" and ts[7] == "-" and ts[10] == " " and ts[13] == ":"
