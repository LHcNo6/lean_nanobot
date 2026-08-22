"""step99: _write_entries 原子写测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from step99.memory import MemoryStore


@pytest.fixture
def store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(workspace=str(tmp_path))


class TestAtomicWrite:
    """_write_entries 原子写测试。"""

    def test_write_entries_creates_file(self, store: MemoryStore) -> None:
        """写入后 history.jsonl 存在且内容正确。"""
        entries = [
            {"cursor": 1, "timestamp": "2026-01-01 00:00", "content": "first"},
            {"cursor": 2, "timestamp": "2026-01-01 00:01", "content": "second"},
        ]
        store._write_entries(entries)
        assert store.history_file.exists()
        lines = store.history_file.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 2
        assert json.loads(lines[0])["content"] == "first"
        assert json.loads(lines[1])["content"] == "second"

    def test_no_tmp_residue(self, store: MemoryStore) -> None:
        """写入完成后无 .tmp 残留文件。"""
        entries = [{"cursor": 1, "timestamp": "2026-01-01 00:00", "content": "test"}]
        store._write_entries(entries)
        tmp_files = list(store.history_file.parent.glob("*.tmp"))
        assert tmp_files == [], f"不应有临时文件残留: {tmp_files}"

    def test_overwrite_replaces_content(self, store: MemoryStore) -> None:
        """多次写入覆盖之前内容。"""
        store._write_entries([{"cursor": 1, "timestamp": "t1", "content": "old"}])
        store._write_entries([
            {"cursor": 2, "timestamp": "t2", "content": "new1"},
            {"cursor": 3, "timestamp": "t3", "content": "new2"},
        ])
        lines = store.history_file.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 2
        assert json.loads(lines[0])["content"] == "new1"

    def test_empty_entries_creates_empty_file(self, store: MemoryStore) -> None:
        """空列表写入创建空文件。"""
        store._write_entries([])
        assert store.history_file.exists()
        assert store.history_file.read_text(encoding="utf-8") == ""

    def test_compact_history_uses_atomic_write(self, store: MemoryStore) -> None:
        """compact_history 通过 _write_entries 原子写入。"""
        # 写入超过 max_history_entries 的条目
        store.max_history_entries = 2
        for i in range(5):
            store.append_history(f"msg-{i}")
        store.compact_history()
        entries = store._read_entries()
        assert len(entries) == 2
        # 无 tmp 残留
        tmp_files = list(store.history_file.parent.glob("*.tmp"))
        assert tmp_files == []

    def test_write_preserves_unicode(self, store: MemoryStore) -> None:
        """写入保留 Unicode 内容。"""
        entries = [{"cursor": 1, "timestamp": "t1", "content": "中文内容 🎉"}]
        store._write_entries(entries)
        read_back = store._read_entries()
        assert read_back[0]["content"] == "中文内容 🎉"
