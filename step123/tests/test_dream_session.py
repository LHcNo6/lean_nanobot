"""step123: dream_session_key + prune_dream_sessions 测试。"""

from __future__ import annotations

import base64
import os
import time
from pathlib import Path

import pytest

from step123.memory import MemoryStore


def _encode_key(key: str) -> str:
    """base64url 编码 session key（无 padding）。"""
    return base64.urlsafe_b64encode(key.encode("utf-8")).decode("utf-8").rstrip("=")


class TestDreamSessionKey:
    """dream_session_key 测试。"""

    def test_returns_dream_prefix(self) -> None:
        """返回 dream: 前缀。"""
        key = MemoryStore.dream_session_key()
        assert key.startswith("dream:")

    def test_contains_timestamp(self) -> None:
        """包含时间戳。"""
        key = MemoryStore.dream_session_key()
        timestamp = key.split(":", 1)[1]
        assert len(timestamp) == 15  # YYYYMMDD-HHMMSS
        assert "-" in timestamp


class TestPruneDreamSessions:
    """prune_dream_sessions 测试。"""

    def test_keeps_non_dream_files(self, tmp_path: Path) -> None:
        """不删除非 dream session 文件。"""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        normal_file = sessions_dir / "normal.jsonl"
        normal_file.write_text("data", encoding="utf-8")
        dream_file = sessions_dir / f"{_encode_key('dream:20260101-000000')}.jsonl"
        dream_file.write_text("data", encoding="utf-8")

        MemoryStore.prune_dream_sessions(sessions_dir, keep=0)
        assert normal_file.exists()
        assert not dream_file.exists()

    def test_keeps_recent_dream_sessions(self, tmp_path: Path) -> None:
        """保留最近的 N 个 dream session。"""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        dream_files = []
        for i in range(5):
            f = sessions_dir / f"{_encode_key(f'dream:2026010{i+1}-000000')}.jsonl"
            f.write_text("data", encoding="utf-8")
            os.utime(f, (time.time() - i * 100, time.time() - i * 100))
            dream_files.append(f)

        MemoryStore.prune_dream_sessions(sessions_dir, keep=2)
        assert dream_files[0].exists()
        assert dream_files[1].exists()
        assert not dream_files[2].exists()
        assert not dream_files[3].exists()
        assert not dream_files[4].exists()

    def test_no_dream_files_does_nothing(self, tmp_path: Path) -> None:
        """无 dream 文件时不做任何操作。"""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        normal_file = sessions_dir / "normal.jsonl"
        normal_file.write_text("data", encoding="utf-8")
        MemoryStore.prune_dream_sessions(sessions_dir, keep=10)
        assert normal_file.exists()

    def test_fewer_than_keep_does_nothing(self, tmp_path: Path) -> None:
        """dream 文件数少于 keep 时不删除。"""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        dream_file = sessions_dir / f"{_encode_key('dream:20260101-000000')}.jsonl"
        dream_file.write_text("data", encoding="utf-8")
        MemoryStore.prune_dream_sessions(sessions_dir, keep=10)
        assert dream_file.exists()
