"""step122: Legacy HISTORY.md 迁移测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from step122.memory import MemoryStore


@pytest.fixture
def store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(workspace=str(tmp_path))


class TestMigrateLegacyHistory:
    """migrate_legacy_history 测试。"""

    def test_no_file_returns_zero(self, store: MemoryStore) -> None:
        """无 HISTORY.md 时返回 0。"""
        assert store.migrate_legacy_history() == 0

    def test_empty_file_returns_zero(self, store: MemoryStore) -> None:
        """空 HISTORY.md 返回 0。"""
        store.legacy_history_file.write_text("", encoding="utf-8")
        assert store.migrate_legacy_history() == 0

    def test_migrates_single_entry(self, store: MemoryStore) -> None:
        """迁移单条记录。"""
        store.legacy_history_file.write_text(
            "[2026-01-01 10:00] test content",
            encoding="utf-8",
        )
        count = store.migrate_legacy_history()
        assert count == 1
        # history.jsonl 应包含条目
        entries = store._read_entries()
        assert len(entries) == 1
        assert entries[0]["content"] == "test content"
        assert entries[0]["timestamp"] == "2026-01-01 10:00"

    def test_migrates_multiple_entries(self, store: MemoryStore) -> None:
        """迁移多条记录（空行分隔）。"""
        store.legacy_history_file.write_text(
            "[2026-01-01 10:00] first entry\n\n"
            "[2026-01-01 11:00] second entry\n\n"
            "[2026-01-01 12:00] third entry",
            encoding="utf-8",
        )
        count = store.migrate_legacy_history()
        assert count == 3

    def test_backs_up_original(self, store: MemoryStore) -> None:
        """迁移后备份原文件。"""
        store.legacy_history_file.write_text(
            "[2026-01-01 10:00] test",
            encoding="utf-8",
        )
        store.migrate_legacy_history()
        # 原文件应被移走
        assert not store.legacy_history_file.exists()
        # 备份文件应存在
        assert store.legacy_history_file.with_suffix(".md.bak").exists()

    def test_sets_cursor_after_migration(self, store: MemoryStore) -> None:
        """迁移后设置 cursor 文件。"""
        store.legacy_history_file.write_text(
            "[2026-01-01 10:00] first\n\n[2026-01-01 11:00] second",
            encoding="utf-8",
        )
        store.migrate_legacy_history()
        assert store._read_cursor_counter() == 2


class TestParseLegacyHistory:
    """_parse_legacy_history 测试。"""

    def test_empty_text_returns_empty(self, store: MemoryStore) -> None:
        """空文本返回空列表。"""
        assert store._parse_legacy_history("") == []

    def test_normalizes_line_endings(self, store: MemoryStore) -> None:
        """标准化换行符。"""
        entries = store._parse_legacy_history("[2026-01-01 10:00] test\r\n")
        assert len(entries) == 1

    def test_without_timestamp_uses_fallback(self, store: MemoryStore) -> None:
        """无时间戳时使用 fallback。"""
        store.legacy_history_file.write_text("content without timestamp", encoding="utf-8")
        entries = store._parse_legacy_history("content without timestamp")
        assert len(entries) == 1
        assert entries[0]["timestamp"]  # 非空


class TestLegacyFallbackTimestamp:
    """_legacy_fallback_timestamp 测试。"""

    def test_returns_valid_format(self, store: MemoryStore) -> None:
        """返回 YYYY-MM-DD HH:MM 格式。"""
        store.legacy_history_file.write_text("test", encoding="utf-8")
        ts = store._legacy_fallback_timestamp()
        assert len(ts) == 16  # YYYY-MM-DD HH:MM
