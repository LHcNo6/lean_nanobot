"""step76：ReadFileTool 升级迁移单元测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from step83.context import ToolContext
from step83.loader import ToolLoader
from step83.tool import ToolRegistry, ToolResult
from step83.tools.filesystem import ReadFileTool


def _make_config() -> SimpleNamespace:
    return SimpleNamespace(
        exec=SimpleNamespace(enable=True, timeout=60, sandbox="", allowed_env_keys=[], allow_patterns=[], deny_patterns=[], path_prepend="", path_append=""),
        tools=SimpleNamespace(restrict_to_workspace=False, file=SimpleNamespace(enable=True)),
        web=SimpleNamespace(enable=True, timeout=30, user_agent="Test", search=SimpleNamespace(provider="duckduckgo", max_results=5, timeout=30)),
        my=SimpleNamespace(enable=True, allow_set=False),
    )


def _make_ctx(workspace: str) -> ToolContext:
    from step83.tools.file_state import FileStateStore
    return ToolContext(
        config=_make_config(),
        workspace=workspace,
        restrict_to_workspace=False,
        session_key="test-session",
        file_state_store=FileStateStore(),
    )


def _run(coro):
    return asyncio.run(coro)


class TestReadFileBasic:
    """基础读取。"""

    def test_read_full_file(self, tmp_path: Path) -> None:
        """读取完整文件，输出 LINE_NUM|CONTENT 格式。"""
        f = tmp_path / "test.py"
        f.write_text("line1\nline2\nline3\n")

        ctx = _make_ctx(str(tmp_path))
        tool = ReadFileTool.create(ctx)

        result = _run(tool.execute(path="test.py"))

        assert "1|line1" in str(result)
        assert "2|line2" in str(result)
        assert "3|line3" in str(result)

    def test_empty_file(self, tmp_path: Path) -> None:
        """空文件返回提示。"""
        f = tmp_path / "empty.py"
        f.write_text("")

        ctx = _make_ctx(str(tmp_path))
        tool = ReadFileTool.create(ctx)

        result = _run(tool.execute(path="empty.py"))
        assert "Empty file" in str(result)

    def test_file_not_found(self, tmp_path: Path) -> None:
        """不存在的文件报错。"""
        ctx = _make_ctx(str(tmp_path))
        tool = ReadFileTool.create(ctx)

        result = _run(tool.execute(path="nonexistent.py"))
        assert isinstance(result, ToolResult)
        assert result.is_error

    def test_missing_path(self, tmp_path: Path) -> None:
        """缺少 path 报错。"""
        ctx = _make_ctx(str(tmp_path))
        tool = ReadFileTool.create(ctx)

        result = _run(tool.execute(path=""))
        assert isinstance(result, ToolResult)
        assert result.is_error


class TestReadFilePagination:
    """行号分页。"""

    def test_offset(self, tmp_path: Path) -> None:
        """offset 从指定行开始。"""
        f = tmp_path / "test.py"
        f.write_text("a\nb\nc\nd\ne\n")

        ctx = _make_ctx(str(tmp_path))
        tool = ReadFileTool.create(ctx)

        result = _run(tool.execute(path="test.py", offset=3))

        assert "1|a" not in str(result)
        assert "3|c" in str(result)
        assert "5|e" in str(result)

    def test_limit(self, tmp_path: Path) -> None:
        """limit 限制返回行数。"""
        f = tmp_path / "test.py"
        f.write_text("a\nb\nc\nd\ne\n")

        ctx = _make_ctx(str(tmp_path))
        tool = ReadFileTool.create(ctx)

        result = _run(tool.execute(path="test.py", limit=2))

        assert "1|a" in str(result)
        assert "2|b" in str(result)
        assert "3|c" not in str(result)

    def test_offset_and_limit(self, tmp_path: Path) -> None:
        """offset + limit 组合。"""
        f = tmp_path / "test.py"
        f.write_text("a\nb\nc\nd\ne\n")

        ctx = _make_ctx(str(tmp_path))
        tool = ReadFileTool.create(ctx)

        result = _run(tool.execute(path="test.py", offset=2, limit=2))

        assert "2|b" in str(result)
        assert "3|c" in str(result)
        assert "1|a" not in str(result)
        assert "4|d" not in str(result)

    def test_offset_beyond_end(self, tmp_path: Path) -> None:
        """offset 超出文件末尾。"""
        f = tmp_path / "test.py"
        f.write_text("a\nb\n")

        ctx = _make_ctx(str(tmp_path))
        tool = ReadFileTool.create(ctx)

        result = _run(tool.execute(path="test.py", offset=10))
        assert "beyond end" in str(result)

    def test_pagination_info(self, tmp_path: Path) -> None:
        """分页时显示行范围信息。"""
        f = tmp_path / "test.py"
        f.write_text("a\nb\nc\nd\ne\n")

        ctx = _make_ctx(str(tmp_path))
        tool = ReadFileTool.create(ctx)

        result = _run(tool.execute(path="test.py", offset=2, limit=2))
        assert "showing lines 2-3 of 5" in str(result)


class TestReadFileTruncation:
    """截断。"""

    def test_max_chars_truncation(self, tmp_path: Path) -> None:
        """max_chars 限制输出长度。"""
        f = tmp_path / "test.py"
        f.write_text("x" * 1000 + "\n")

        ctx = _make_ctx(str(tmp_path))
        tool = ReadFileTool.create(ctx)

        result = _run(tool.execute(path="test.py", max_chars=100))
        assert "truncated" in str(result)


class TestReadFileDiscovery:
    """工具发现。"""

    def test_discovered_from_filesystem(self, tmp_path: Path) -> None:
        """ToolLoader 从 filesystem.py 发现 read_file。"""
        ctx = _make_ctx(str(tmp_path))
        registry = ToolRegistry()
        loaded = ToolLoader().load(ctx, registry, scope="core")

        assert "read_file" in loaded
        assert registry.has("read_file")

    def test_reexport_compatible(self) -> None:
        """旧导入路径仍可用（re-export）。"""
        from step83.tools.read_file import ReadFileTool as OldReadFileTool
        from step83.tools.filesystem import ReadFileTool as NewReadFileTool

        assert OldReadFileTool is NewReadFileTool

    def test_schema(self, tmp_path: Path) -> None:
        """参数 schema 包含 offset/limit。"""
        ctx = _make_ctx(str(tmp_path))
        tool = ReadFileTool.create(ctx)
        schema = tool.to_schema()

        assert schema["function"]["name"] == "read_file"
        props = schema["function"]["parameters"]["properties"]
        assert "path" in props
        assert "offset" in props
        assert "limit" in props
        assert "max_chars" in props
