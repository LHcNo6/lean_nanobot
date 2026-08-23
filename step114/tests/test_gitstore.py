"""step114: gitstore 模块测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from step114.utils.gitstore import CommitInfo, GitStore


@pytest.fixture
def gitstore(tmp_path: Path) -> GitStore:
    """创建 GitStore 实例。"""
    tracked = ["memory/MEMORY.md", "SOUL.md", "USER.md"]
    return GitStore(workspace=tmp_path, tracked_files=tracked)


class TestGitStoreInit:
    """GitStore 初始化测试。"""

    def test_not_initialized_by_default(self, gitstore: GitStore) -> None:
        """默认未初始化。"""
        assert gitstore.is_initialized() is False

    def test_init_creates_git_dir(self, gitstore: GitStore) -> None:
        """init 创建 .git 目录。"""
        result = gitstore.init()
        assert result is True
        assert gitstore.is_initialized() is True

    def test_init_idempotent(self, gitstore: GitStore) -> None:
        """重复 init 返回 False。"""
        assert gitstore.init() is True
        assert gitstore.init() is False

    def test_init_creates_tracked_files(self, gitstore: GitStore) -> None:
        """init 创建 tracked 文件。"""
        gitstore.init()
        for rel in gitstore._tracked_files:
            assert (gitstore._workspace / rel).exists()


class TestGitStoreAutoCommit:
    """auto_commit 测试。"""

    def test_no_changes_returns_none(self, gitstore: GitStore) -> None:
        """无变更时返回 None。"""
        gitstore.init()
        result = gitstore.auto_commit("test commit")
        assert result is None

    def test_with_changes_returns_sha(self, gitstore: GitStore) -> None:
        """有变更时返回短 SHA。"""
        gitstore.init()
        # 修改一个 tracked 文件
        mem_file = gitstore._workspace / "memory/MEMORY.md"
        mem_file.write_text("new content", encoding="utf-8")
        result = gitstore.auto_commit("update memory")
        assert result is not None
        assert len(result) == 8  # short SHA

    def test_not_initialized_returns_none(self, gitstore: GitStore) -> None:
        """未初始化时返回 None。"""
        result = gitstore.auto_commit("test")
        assert result is None


class TestGitStoreSummarize:
    """summarize_working_tree 测试。"""

    def test_no_changes_returns_empty(self, gitstore: GitStore) -> None:
        """无变更时返回空字符串。"""
        gitstore.init()
        result = gitstore.summarize_working_tree()
        assert result == ""

    def test_with_changes_returns_summary(self, gitstore: GitStore) -> None:
        """有变更时返回变更摘要。"""
        gitstore.init()
        mem_file = gitstore._workspace / "memory/MEMORY.md"
        mem_file.write_text("modified", encoding="utf-8")
        result = gitstore.summarize_working_tree()
        assert "MEMORY.md" in result

    def test_not_initialized_returns_empty(self, gitstore: GitStore) -> None:
        """未初始化时返回空字符串。"""
        result = gitstore.summarize_working_tree()
        assert result == ""


class TestCommitInfo:
    """CommitInfo 测试。"""

    def test_subject_returns_first_line(self) -> None:
        """subject 返回第一行。"""
        info = CommitInfo(sha="abc12345", message="first line\nsecond", timestamp="2026-01-01")
        assert info.subject() == "first line"

    def test_subject_empty_message(self) -> None:
        """空 message 返回占位符。"""
        info = CommitInfo(sha="abc12345", message="", timestamp="2026-01-01")
        assert info.subject() == "(no message)"
