"""step120: MemoryStore 持久化文件读写方法测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from step120.memory import MemoryStore


@pytest.fixture
def store(tmp_path: Path) -> MemoryStore:
    """创建一个基于临时目录的 MemoryStore 实例。"""
    return MemoryStore(workspace=str(tmp_path))


class TestReadFile:
    """read_file 静态方法测试。"""

    def test_file_not_found_returns_empty(self, tmp_path: Path) -> None:
        """文件不存在时返回空字符串。"""
        missing = tmp_path / "nonexistent.md"
        assert MemoryStore.read_file(missing) == ""

    def test_file_exists_returns_content(self, tmp_path: Path) -> None:
        """文件存在时返回 UTF-8 内容。"""
        path = tmp_path / "test.md"
        path.write_text("hello 世界", encoding="utf-8")
        assert MemoryStore.read_file(path) == "hello 世界"

    def test_empty_file_returns_empty_string(self, tmp_path: Path) -> None:
        """空文件返回空字符串。"""
        path = tmp_path / "empty.md"
        path.write_text("", encoding="utf-8")
        assert MemoryStore.read_file(path) == ""


class TestMemoryFile:
    """MEMORY.md 读写测试。"""

    def test_read_memory_not_exists(self, store: MemoryStore) -> None:
        """MEMORY.md 不存在时返回空串。"""
        assert store.read_memory() == ""

    def test_write_then_read_memory(self, store: MemoryStore) -> None:
        """写入后读取内容一致。"""
        store.write_memory("# Long-term Memory\n- fact 1")
        assert store.read_memory() == "# Long-term Memory\n- fact 1"

    def test_write_memory_overwrites(self, store: MemoryStore) -> None:
        """多次写入覆盖之前内容。"""
        store.write_memory("old content")
        store.write_memory("new content")
        assert store.read_memory() == "new content"

    def test_write_memory_unicode(self, store: MemoryStore) -> None:
        """支持中文等 Unicode 内容。"""
        content = "用户偏好：中文回复\n记忆：喜欢简洁"
        store.write_memory(content)
        assert store.read_memory() == content


class TestSoulFile:
    """SOUL.md 读写测试。"""

    def test_read_soul_not_exists(self, store: MemoryStore) -> None:
        """SOUL.md 不存在时返回空串。"""
        assert store.read_soul() == ""

    def test_write_then_read_soul(self, store: MemoryStore) -> None:
        """写入后读取内容一致。"""
        store.write_soul("# Soul\nYou are a helpful assistant.")
        assert store.read_soul() == "# Soul\nYou are a helpful assistant."

    def test_write_soul_overwrites(self, store: MemoryStore) -> None:
        """多次写入覆盖。"""
        store.write_soul("old")
        store.write_soul("new")
        assert store.read_soul() == "new"


class TestUserFile:
    """USER.md 读写测试。"""

    def test_read_user_not_exists(self, store: MemoryStore) -> None:
        """USER.md 不存在时返回空串。"""
        assert store.read_user() == ""

    def test_write_then_read_user(self, store: MemoryStore) -> None:
        """写入后读取内容一致。"""
        store.write_user("# User\nName: Test")
        assert store.read_user() == "# User\nName: Test"

    def test_write_user_overwrites(self, store: MemoryStore) -> None:
        """多次写入覆盖。"""
        store.write_user("old")
        store.write_user("new")
        assert store.read_user() == "new"


class TestFileLocations:
    """文件路径正确性测试。"""

    def test_memory_file_location(self, store: MemoryStore, tmp_path: Path) -> None:
        """MEMORY.md 位于 memory/ 子目录下。"""
        assert store.memory_file == tmp_path / "memory" / "MEMORY.md"

    def test_soul_file_location(self, store: MemoryStore, tmp_path: Path) -> None:
        """SOUL.md 位于 workspace 根目录。"""
        assert store.soul_file == tmp_path / "SOUL.md"

    def test_user_file_location(self, store: MemoryStore, tmp_path: Path) -> None:
        """USER.md 位于 workspace 根目录。"""
        assert store.user_file == tmp_path / "USER.md"

    def test_write_creates_memory_dir(self, store: MemoryStore, tmp_path: Path) -> None:
        """写入 MEMORY.md 时 memory/ 目录已存在（__init__ 中 ensure_dir）。"""
        store.write_memory("test")
        assert (tmp_path / "memory" / "MEMORY.md").exists()
