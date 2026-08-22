"""step77：GlobTool 单元测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from step110.context import ToolContext
from step110.loader import ToolLoader
from step110.tool import ToolRegistry, ToolResult
from step110.tools.glob_tool import GlobTool


def _make_config() -> SimpleNamespace:
    return SimpleNamespace(
        exec=SimpleNamespace(enable=True, timeout=60, sandbox="", allowed_env_keys=[], allow_patterns=[], deny_patterns=[], path_prepend="", path_append=""),
        tools=SimpleNamespace(restrict_to_workspace=False, file=SimpleNamespace(enable=True)),
        web=SimpleNamespace(enable=True, timeout=30, user_agent="Test", search=SimpleNamespace(provider="duckduckgo", max_results=5, timeout=30)),
        my=SimpleNamespace(enable=True, allow_set=False),
    )


def _make_ctx(workspace: str) -> ToolContext:
    from step110.tools.file_state import FileStateStore
    return ToolContext(
        config=_make_config(),
        workspace=workspace,
        restrict_to_workspace=False,
        session_key="test-session",
        file_state_store=FileStateStore(),
    )


def _run(coro):
    return asyncio.run(coro)


class TestGlobBasic:
    """基础匹配。"""

    def test_match_py_files(self, tmp_path: Path) -> None:
        """匹配当前目录 .py 文件。"""
        (tmp_path / "a.py").write_text("")
        (tmp_path / "b.py").write_text("")
        (tmp_path / "c.txt").write_text("")

        ctx = _make_ctx(str(tmp_path))
        tool = GlobTool.create(ctx)

        result = _run(tool.execute(pattern="*.py"))

        assert "a.py" in str(result)
        assert "b.py" in str(result)
        assert "c.txt" not in str(result)
        assert "2 file(s)" in str(result)

    def test_recursive_match(self, tmp_path: Path) -> None:
        """** 递归匹配。"""
        (tmp_path / "a.py").write_text("")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "b.py").write_text("")

        ctx = _make_ctx(str(tmp_path))
        tool = GlobTool.create(ctx)

        result = _run(tool.execute(pattern="**/*.py"))

        assert "a.py" in str(result)
        assert "sub/b.py" in str(result)

    def test_prefix_match(self, tmp_path: Path) -> None:
        """前缀匹配 test_*.py。"""
        (tmp_path / "test_a.py").write_text("")
        (tmp_path / "test_b.py").write_text("")
        (tmp_path / "main.py").write_text("")

        ctx = _make_ctx(str(tmp_path))
        tool = GlobTool.create(ctx)

        result = _run(tool.execute(pattern="test_*.py"))

        assert "test_a.py" in str(result)
        assert "test_b.py" in str(result)
        assert "main.py" not in str(result)

    def test_no_results(self, tmp_path: Path) -> None:
        """无匹配结果。"""
        ctx = _make_ctx(str(tmp_path))
        tool = GlobTool.create(ctx)

        result = _run(tool.execute(pattern="*.nonexistent"))
        assert "No files found" in str(result)

    def test_missing_pattern(self, tmp_path: Path) -> None:
        """缺少 pattern 报错。"""
        ctx = _make_ctx(str(tmp_path))
        tool = GlobTool.create(ctx)

        result = _run(tool.execute(pattern=""))
        assert isinstance(result, ToolResult)
        assert result.is_error


class TestGlobLimits:
    """结果限制。"""

    def test_max_results(self, tmp_path: Path) -> None:
        """max_results 限制输出。"""
        for i in range(10):
            (tmp_path / f"file{i}.py").write_text("")

        ctx = _make_ctx(str(tmp_path))
        tool = GlobTool.create(ctx)

        result = _run(tool.execute(pattern="*.py", max_results=3))
        assert "truncated" in str(result)
        assert "first 3 of 10" in str(result)


class TestGlobDiscovery:
    """工具发现。"""

    def test_discovered(self, tmp_path: Path) -> None:
        """ToolLoader 自动发现 glob。"""
        ctx = _make_ctx(str(tmp_path))
        registry = ToolRegistry()
        loaded = ToolLoader().load(ctx, registry, scope="core")

        assert "glob" in loaded
        assert registry.has("glob")

    def test_schema(self, tmp_path: Path) -> None:
        """参数 schema 正确。"""
        ctx = _make_ctx(str(tmp_path))
        tool = GlobTool.create(ctx)
        schema = tool.to_schema()

        assert schema["function"]["name"] == "glob"
        props = schema["function"]["parameters"]["properties"]
        assert "pattern" in props
        assert "path" in props
        assert "max_results" in props
