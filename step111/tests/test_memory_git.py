"""step111: MemoryStore Git 集成 + dream_content_diff 测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from step111.memory import MemoryStore
from step111.utils.gitstore import GitStore


@pytest.fixture
def store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(workspace=str(tmp_path))


class TestMemoryStoreGitIntegration:
    """MemoryStore Git 集成测试。"""

    def test_git_property_returns_gitstore(self, store: MemoryStore) -> None:
        """git property 返回 GitStore 实例。"""
        assert isinstance(store.git, GitStore)

    def test_git_not_initialized_by_default(self, store: MemoryStore) -> None:
        """默认 git 未初始化。"""
        assert store.git.is_initialized() is False

    def test_git_can_be_initialized(self, store: MemoryStore) -> None:
        """git 可以初始化。"""
        result = store.git.init()
        assert result is True
        assert store.git.is_initialized() is True


class TestDreamContentDiff:
    """dream_content_diff 测试。"""

    def test_returns_empty_when_git_not_initialized(self, store: MemoryStore) -> None:
        """git 未初始化时返回空字符串。"""
        assert store.dream_content_diff() == ""

    def test_returns_empty_when_no_changes(self, store: MemoryStore) -> None:
        """无变更时返回空字符串。"""
        store.git.init()
        assert store.dream_content_diff() == ""

    def test_returns_summary_when_changes(self, store: MemoryStore) -> None:
        """有变更时返回变更摘要。"""
        store.git.init()
        # 修改记忆文件
        store.write_memory("new memory content")
        result = store.dream_content_diff()
        assert "MEMORY.md" in result

    def test_returns_empty_after_commit(self, store: MemoryStore) -> None:
        """提交后返回空字符串。"""
        store.git.init()
        store.write_memory("new content")
        store.git.auto_commit("update memory")
        assert store.dream_content_diff() == ""
