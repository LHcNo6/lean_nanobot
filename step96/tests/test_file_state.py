"""step39 测试：file_state contextvar 绑定 + FileStates 追踪。

全构造数据：tmp_path 临时文件；无真实 API。
覆盖：
- FileStates：record_read / record_write / check_read / is_unchanged / get / clear；
- FileStateStore：for_session 创建/复用 / clear；
- ContextVar：bind / reset / current_file_states default。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from step96.tools.file_state import (
    FileStateStore,
    FileStates,
    bind_file_states,
    current_file_states,
    reset_file_states,
)


# ---------------------------------------------------------------------------
# TestFileStatesRecordRead
# ---------------------------------------------------------------------------


class TestFileStatesRecordRead:
    """FileStates.record_read。"""

    def test_record_read_stores_state(self, tmp_path: Path) -> None:
        """record_read 后 get 返回 ReadState。"""
        p = tmp_path / "test.txt"
        p.write_text("hello", encoding="utf-8")
        fs = FileStates()
        fs.record_read(p)
        state = fs.get(p)
        assert state is not None
        assert state.can_dedup is True
        assert state.content_hash is not None

    def test_record_read_with_offset_limit(self, tmp_path: Path) -> None:
        """record_read 记录 offset 和 limit。"""
        p = tmp_path / "test.txt"
        p.write_text("line1\nline2\nline3\n", encoding="utf-8")
        fs = FileStates()
        fs.record_read(p, offset=2, limit=1)
        state = fs.get(p)
        assert state is not None
        assert state.offset == 2
        assert state.limit == 1

    def test_record_read_nonexistent_file_no_state(self, tmp_path: Path) -> None:
        """不存在的文件 record_read 不存储状态。"""
        fs = FileStates()
        fs.record_read(tmp_path / "nonexistent.txt")
        assert fs.get(tmp_path / "nonexistent.txt") is None


# ---------------------------------------------------------------------------
# TestFileStatesRecordWrite
# ---------------------------------------------------------------------------


class TestFileStatesRecordWrite:
    """FileStates.record_write。"""

    def test_record_write_marks_not_dedupable(self, tmp_path: Path) -> None:
        """record_write 后 can_dedup=False。"""
        p = tmp_path / "test.txt"
        p.write_text("hello", encoding="utf-8")
        fs = FileStates()
        fs.record_read(p)
        assert fs.get(p).can_dedup is True  # type: ignore[union-attr]
        fs.record_write(p)
        assert fs.get(p).can_dedup is False  # type: ignore[union-attr]

    def test_record_write_nonexistent_removes_state(self, tmp_path: Path) -> None:
        """写入不存在的文件时移除状态。"""
        p = tmp_path / "test.txt"
        p.write_text("hello", encoding="utf-8")
        fs = FileStates()
        fs.record_read(p)
        assert fs.get(p) is not None
        # 删除文件后 record_write
        p.unlink()
        fs.record_write(p)
        assert fs.get(p) is None


# ---------------------------------------------------------------------------
# TestFileStatesCheckRead
# ---------------------------------------------------------------------------


class TestFileStatesCheckRead:
    """FileStates.check_read（read-before-edit 警告）。"""

    def test_check_read_unread_file_returns_warning(self, tmp_path: Path) -> None:
        """未读文件返回警告。"""
        p = tmp_path / "test.txt"
        p.write_text("hello", encoding="utf-8")
        fs = FileStates()
        warning = fs.check_read(p)
        assert warning is not None
        assert "not been read" in warning

    def test_check_read_fresh_file_returns_none(self, tmp_path: Path) -> None:
        """已读且未修改返回 None。"""
        p = tmp_path / "test.txt"
        p.write_text("hello", encoding="utf-8")
        fs = FileStates()
        fs.record_read(p)
        assert fs.check_read(p) is None

    def test_check_read_modified_file_returns_warning(self, tmp_path: Path) -> None:
        """已读后修改返回警告。"""
        p = tmp_path / "test.txt"
        p.write_text("hello", encoding="utf-8")
        fs = FileStates()
        fs.record_read(p)
        # 修改文件
        time.sleep(0.01)
        p.write_text("world", encoding="utf-8")
        warning = fs.check_read(p)
        assert warning is not None
        assert "modified since last read" in warning


# ---------------------------------------------------------------------------
# TestFileStatesIsUnchanged
# ---------------------------------------------------------------------------


class TestFileStatesIsUnchanged:
    """FileStates.is_unchanged（read dedup）。"""

    def test_is_unchanged_same_params(self, tmp_path: Path) -> None:
        """相同参数读取返回 True。"""
        p = tmp_path / "test.txt"
        p.write_text("hello", encoding="utf-8")
        fs = FileStates()
        fs.record_read(p, offset=1, limit=None)
        assert fs.is_unchanged(p, offset=1, limit=None) is True

    def test_is_unchanged_different_offset(self, tmp_path: Path) -> None:
        """不同 offset 返回 False。"""
        p = tmp_path / "test.txt"
        p.write_text("hello", encoding="utf-8")
        fs = FileStates()
        fs.record_read(p, offset=1)
        assert fs.is_unchanged(p, offset=2) is False

    def test_is_unchanged_after_write_returns_false(self, tmp_path: Path) -> None:
        """写入后返回 False。"""
        p = tmp_path / "test.txt"
        p.write_text("hello", encoding="utf-8")
        fs = FileStates()
        fs.record_read(p)
        fs.record_write(p)
        assert fs.is_unchanged(p) is False

    def test_is_unchanged_unread_file_returns_false(self, tmp_path: Path) -> None:
        """未读文件返回 False。"""
        p = tmp_path / "test.txt"
        p.write_text("hello", encoding="utf-8")
        fs = FileStates()
        assert fs.is_unchanged(p) is False


# ---------------------------------------------------------------------------
# TestFileStateStore
# ---------------------------------------------------------------------------


class TestFileStateStore:
    """FileStateStore。"""

    def test_for_session_creates_new(self) -> None:
        """for_session 创建新 FileStates。"""
        store = FileStateStore()
        fs = store.for_session("session1")
        assert isinstance(fs, FileStates)

    def test_for_session_returns_same(self) -> None:
        """同 key 返回同一实例。"""
        store = FileStateStore()
        fs1 = store.for_session("session1")
        fs2 = store.for_session("session1")
        assert fs1 is fs2

    def test_for_session_different_keys_different_instances(self) -> None:
        """不同 key 返回不同实例。"""
        store = FileStateStore()
        fs1 = store.for_session("session1")
        fs2 = store.for_session("session2")
        assert fs1 is not fs2

    def test_for_session_none_uses_default(self) -> None:
        """None key 使用 "__default__"。"""
        store = FileStateStore()
        fs1 = store.for_session(None)
        fs2 = store.for_session(None)
        assert fs1 is fs2

    def test_clear(self) -> None:
        """clear 清空所有会话状态。"""
        store = FileStateStore()
        store.for_session("session1")
        store.clear()
        # clear 后重新创建
        fs = store.for_session("session1")
        assert isinstance(fs, FileStates)


# ---------------------------------------------------------------------------
# TestContextVarBinding
# ---------------------------------------------------------------------------


class TestContextVarBinding:
    """file_state ContextVar 绑定。"""

    def test_bind_and_current(self) -> None:
        """bind 后 current_file_states 返回绑定实例。"""
        fs = FileStates()
        token = bind_file_states(fs)
        try:
            assert current_file_states(FileStates()) is fs
        finally:
            reset_file_states(token)

    def test_reset_restores_previous(self) -> None:
        """reset 后恢复之前的绑定（无绑定时返回 default）。"""
        default = FileStates()
        # 初始无绑定，current 返回 default
        assert current_file_states(default) is default

        fs = FileStates()
        token = bind_file_states(fs)
        assert current_file_states(default) is fs

        reset_file_states(token)
        # reset 后恢复无绑定状态
        assert current_file_states(default) is default

    def test_current_file_states_default_when_unbound(self) -> None:
        """无绑定时返回 default。"""
        default = FileStates()
        assert current_file_states(default) is default

    def test_nested_binding(self) -> None:
        """嵌套绑定：内层覆盖外层，reset 后恢复外层。"""
        outer = FileStates()
        inner = FileStates()
        default = FileStates()

        outer_token = bind_file_states(outer)
        try:
            assert current_file_states(default) is outer
            inner_token = bind_file_states(inner)
            try:
                assert current_file_states(default) is inner
            finally:
                reset_file_states(inner_token)
            assert current_file_states(default) is outer
        finally:
            reset_file_states(outer_token)
