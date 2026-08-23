"""step124: build_dream_tools 测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from step124.memory import MemoryStore


@pytest.fixture
def store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(workspace=str(tmp_path))


class TestBuildDreamTools:
    """build_dream_tools 测试。"""

    def test_returns_list(self, store: MemoryStore) -> None:
        """返回列表。"""
        tools = store.build_dream_tools()
        assert isinstance(tools, list)

    def test_contains_read_file(self, store: MemoryStore) -> None:
        """包含 read_file 工具。"""
        tools = store.build_dream_tools()
        names = [t["function"]["name"] for t in tools]
        assert "read_file" in names

    def test_contains_write_file(self, store: MemoryStore) -> None:
        """包含 write_file 工具。"""
        tools = store.build_dream_tools()
        names = [t["function"]["name"] for t in tools]
        assert "write_file" in names

    def test_contains_edit_file(self, store: MemoryStore) -> None:
        """包含 edit_file 工具。"""
        tools = store.build_dream_tools()
        names = [t["function"]["name"] for t in tools]
        assert "edit_file" in names

    def test_no_shell_or_network_tools(self, store: MemoryStore) -> None:
        """不包含 shell 或网络工具。"""
        tools = store.build_dream_tools()
        names = [t["function"]["name"] for t in tools]
        assert "shell" not in names
        assert "exec" not in names
        assert "web_search" not in names

    def test_tools_have_required_fields(self, store: MemoryStore) -> None:
        """每个工具有 type/function/name/parameters 字段。"""
        tools = store.build_dream_tools()
        for tool in tools:
            assert tool["type"] == "function"
            assert "name" in tool["function"]
            assert "parameters" in tool["function"]
            assert tool["function"]["parameters"]["type"] == "object"
