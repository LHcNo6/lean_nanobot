"""step121: 数据校验层 + cursor 对齐测试。"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from step121.memory import MemoryStore


@pytest.fixture
def store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(workspace=str(tmp_path))


def _write_raw_entries(store: MemoryStore, entries: list[dict]) -> None:
    """直接写入原始条目（绕过 append_history 的校验）。"""
    with open(store.history_file, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


class TestValidCursor:
    """_valid_cursor 静态方法测试。"""

    def test_positive_int(self) -> None:
        assert MemoryStore._valid_cursor(5) == 5

    def test_zero(self) -> None:
        assert MemoryStore._valid_cursor(0) == 0

    def test_negative_int_rejected(self) -> None:
        assert MemoryStore._valid_cursor(-1) is None

    def test_bool_rejected(self) -> None:
        """bool 必须被拒绝（isinstance(True, int) 为 True 的陷阱）。"""
        assert MemoryStore._valid_cursor(True) is None
        assert MemoryStore._valid_cursor(False) is None

    def test_none_rejected(self) -> None:
        assert MemoryStore._valid_cursor(None) is None

    def test_string_rejected(self) -> None:
        assert MemoryStore._valid_cursor("5") is None

    def test_float_rejected(self) -> None:
        assert MemoryStore._valid_cursor(5.0) is None


class TestValidHistoryPayload:
    """_valid_history_payload 静态方法测试。"""

    def test_valid_entry(self) -> None:
        entry = {"cursor": 1, "timestamp": "2026-01-01 00:00", "content": "test"}
        assert MemoryStore._valid_history_payload(entry) is True

    def test_valid_with_session_key(self) -> None:
        entry = {"cursor": 1, "timestamp": "t", "content": "c", "session_key": "cli:1"}
        assert MemoryStore._valid_history_payload(entry) is True

    def test_missing_timestamp(self) -> None:
        entry = {"cursor": 1, "content": "test"}
        assert MemoryStore._valid_history_payload(entry) is False

    def test_missing_content(self) -> None:
        entry = {"cursor": 1, "timestamp": "t"}
        assert MemoryStore._valid_history_payload(entry) is False

    def test_timestamp_wrong_type(self) -> None:
        entry = {"cursor": 1, "timestamp": 123, "content": "c"}
        assert MemoryStore._valid_history_payload(entry) is False

    def test_content_wrong_type(self) -> None:
        entry = {"cursor": 1, "timestamp": "t", "content": 123}
        assert MemoryStore._valid_history_payload(entry) is False

    def test_session_key_wrong_type(self) -> None:
        entry = {"cursor": 1, "timestamp": "t", "content": "c", "session_key": 123}
        assert MemoryStore._valid_history_payload(entry) is False


class TestIterValidEntries:
    """_iter_valid_entries 测试。"""

    def test_all_valid(self, store: MemoryStore) -> None:
        _write_raw_entries(store, [
            {"cursor": 1, "timestamp": "t1", "content": "c1"},
            {"cursor": 2, "timestamp": "t2", "content": "c2"},
        ])
        result = list(store._iter_valid_entries())
        assert len(result) == 2
        assert result[0][1] == 1
        assert result[1][1] == 2

    def test_invalid_cursor_skipped(self, store: MemoryStore) -> None:
        _write_raw_entries(store, [
            {"cursor": 1, "timestamp": "t1", "content": "c1"},
            {"cursor": "bad", "timestamp": "t2", "content": "c2"},
            {"cursor": 3, "timestamp": "t3", "content": "c3"},
        ])
        result = list(store._iter_valid_entries())
        assert len(result) == 2
        cursors = [c for _, c in result]
        assert cursors == [1, 3]

    def test_malformed_payload_skipped(self, store: MemoryStore) -> None:
        _write_raw_entries(store, [
            {"cursor": 1, "timestamp": "t1", "content": "c1"},
            {"cursor": 2, "content": "missing timestamp"},
            {"cursor": 3, "timestamp": "t3", "content": "c3"},
        ])
        result = list(store._iter_valid_entries())
        assert len(result) == 2

    def test_bool_cursor_skipped(self, store: MemoryStore) -> None:
        _write_raw_entries(store, [
            {"cursor": True, "timestamp": "t1", "content": "c1"},
            {"cursor": 2, "timestamp": "t2", "content": "c2"},
        ])
        result = list(store._iter_valid_entries())
        assert len(result) == 1
        assert result[0][1] == 2

    def test_warning_only_once(self, store: MemoryStore, caplog: pytest.LogCaptureFixture) -> None:
        """无效 cursor 警告只输出一次。"""
        _write_raw_entries(store, [
            {"cursor": "bad1", "timestamp": "t", "content": "c"},
            {"cursor": "bad2", "timestamp": "t", "content": "c"},
            {"cursor": "bad3", "timestamp": "t", "content": "c"},
        ])
        with caplog.at_level(logging.WARNING):
            list(store._iter_valid_entries())
        warning_count = sum(
            1 for r in caplog.records if "invalid cursor" in r.message
        )
        assert warning_count == 1

    def test_empty_file(self, store: MemoryStore) -> None:
        result = list(store._iter_valid_entries())
        assert result == []


class TestReadUnprocessedHistory:
    """read_unprocessed_history 改用校验层测试。"""

    def test_filters_invalid_entries(self, store: MemoryStore) -> None:
        _write_raw_entries(store, [
            {"cursor": 1, "timestamp": "t1", "content": "c1"},
            {"cursor": "bad", "timestamp": "t2", "content": "c2"},
            {"cursor": 3, "timestamp": "t3", "content": "c3"},
        ])
        result = store.read_unprocessed_history(since_cursor=0)
        assert len(result) == 2
        assert result[0]["cursor"] == 1
        assert result[1]["cursor"] == 3

    def test_since_cursor_filter(self, store: MemoryStore) -> None:
        _write_raw_entries(store, [
            {"cursor": 1, "timestamp": "t1", "content": "c1"},
            {"cursor": 2, "timestamp": "t2", "content": "c2"},
            {"cursor": 3, "timestamp": "t3", "content": "c3"},
        ])
        result = store.read_unprocessed_history(since_cursor=1)
        assert len(result) == 2
        assert result[0]["cursor"] == 2


class TestNextCursor:
    """_next_cursor 对齐测试。"""

    def test_empty_history(self, store: MemoryStore) -> None:
        assert store._next_cursor() == 1

    def test_after_appends(self, store: MemoryStore) -> None:
        store.append_history("first")
        store.append_history("second")
        assert store._next_cursor() == 3

    def test_with_invalid_entries(self, store: MemoryStore) -> None:
        """有无效条目时 cursor 仍单调递增。"""
        _write_raw_entries(store, [
            {"cursor": 1, "timestamp": "t1", "content": "c1"},
            {"cursor": "bad", "timestamp": "t2", "content": "c2"},
            {"cursor": 5, "timestamp": "t3", "content": "c3"},
        ])
        assert store._next_cursor() == 6

    def test_with_cursor_file(self, store: MemoryStore) -> None:
        """cursor 文件优先。"""
        store._cursor_file.write_text("10", encoding="utf-8")
        _write_raw_entries(store, [
            {"cursor": 1, "timestamp": "t1", "content": "c1"},
        ])
        assert store._next_cursor() == 11


class TestGetLatestCursor:
    """get_latest_cursor 对齐测试。"""

    def test_empty_history(self, store: MemoryStore) -> None:
        assert store.get_latest_cursor() == 0

    def test_after_appends(self, store: MemoryStore) -> None:
        store.append_history("first")
        store.append_history("second")
        assert store.get_latest_cursor() == 2

    def test_with_invalid_entries(self, store: MemoryStore) -> None:
        _write_raw_entries(store, [
            {"cursor": 1, "timestamp": "t1", "content": "c1"},
            {"cursor": "bad", "timestamp": "t2", "content": "c2"},
            {"cursor": 5, "timestamp": "t3", "content": "c3"},
        ])
        assert store.get_latest_cursor() == 5
