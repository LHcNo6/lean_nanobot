"""step68：FindFilesTool + GrepTool 单元测试。

覆盖：
- FindFilesTool: query/glob/type 过滤、噪声目录、截断、无结果
- GrepTool: content/files_with_matches 模式、大小写、固定字符串、glob、二进制跳过、无效正则
- 工具发现
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from step76.context import ToolContext
from step76.loader import ToolLoader
from step76.tool import ToolRegistry, ToolResult
from step76.tools.file_state import FileStateStore
from step76.tools.search import FindFilesTool, GrepTool, _is_binary, _match_glob, _matches_type


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _make_config(*, restrict: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        tools=SimpleNamespace(
            restrict_to_workspace=restrict,
            file=SimpleNamespace(enable=True),
        )
    )


def _make_ctx(workspace: str, *, restrict: bool = False) -> ToolContext:
    return ToolContext(
        config=_make_config(restrict=restrict),
        workspace=workspace,
        restrict_to_workspace=restrict,
        file_state_store=FileStateStore(),
        session_key="test-session",
    )


def _run(coro):
    return asyncio.run(coro)


def _make_tree(root: Path, structure: dict) -> None:
    for name, value in structure.items():
        path = root / name
        if isinstance(value, dict):
            path.mkdir(parents=True, exist_ok=True)
            _make_tree(path, value)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(value), encoding="utf-8")


# ---------------------------------------------------------------------------
# 辅助函数测试
# ---------------------------------------------------------------------------


class TestHelpers:
    """搜索辅助函数。"""

    def test_is_binary_null_byte(self) -> None:
        """含 null 字节 → 二进制。"""
        assert _is_binary(b"hello\x00world") is True

    def test_is_binary_text(self) -> None:
        """纯文本 → 非二进制。"""
        assert _is_binary(b"hello world\nline2\n") is False

    def test_is_binary_empty(self) -> None:
        """空内容 → 非二进制。"""
        assert _is_binary(b"") is False

    def test_match_glob_filename(self) -> None:
        """glob 匹配文件名。"""
        assert _match_glob("src/main.py", "main.py", "*.py") is True
        assert _match_glob("src/main.py", "main.py", "*.txt") is False

    def test_match_glob_path(self) -> None:
        """glob 含 / 时匹配完整路径。"""
        # 单级匹配
        assert _match_glob("tests/test_main.py", "test_main.py", "tests/*.py") is True
        # ** 匹配一个或多个目录
        assert _match_glob("tests/sub/test_main.py", "test_main.py", "tests/**/*.py") is True
        # 不匹配的情况
        assert _match_glob("src/main.py", "main.py", "tests/**/*.py") is False

    def test_matches_type(self) -> None:
        """文件类型简写匹配。"""
        assert _matches_type("main.py", "py") is True
        assert _matches_type("main.py", "js") is False
        assert _matches_type("main.py", None) is True
        assert _matches_type("main.py", "") is True

    def test_matches_type_unknown(self) -> None:
        """未知类型自动用 *.{type} 匹配。"""
        assert _matches_type("data.xyz", "xyz") is True
        assert _matches_type("data.abc", "xyz") is False


# ---------------------------------------------------------------------------
# FindFilesTool
# ---------------------------------------------------------------------------


class TestFindFilesTool:
    """FindFilesTool 文件查找。"""

    def test_find_by_query(self, tmp_path: Path) -> None:
        """按路径片段查找。"""
        _make_tree(tmp_path, {
            "src": {"main.py": "pass", "utils.py": "pass"},
            "tests": {"test_main.py": "pass"},
            "README.md": "# test",
        })

        ctx = _make_ctx(str(tmp_path))
        tool = FindFilesTool.create(ctx)

        result = _run(tool.execute(path=str(tmp_path), query="main"))
        lines = str(result).split("\n")

        assert any("main.py" in l for l in lines)
        assert any("test_main.py" in l for l in lines)
        assert not any("utils.py" in l for l in lines)

    def test_find_by_glob(self, tmp_path: Path) -> None:
        """按 glob 模式过滤。"""
        _make_tree(tmp_path, {
            "src": {"a.py": "pass", "b.js": "pass"},
            "doc.md": "# doc",
        })

        ctx = _make_ctx(str(tmp_path))
        tool = FindFilesTool.create(ctx)

        result = _run(tool.execute(path=str(tmp_path), glob="*.py"))
        lines = str(result).split("\n")

        assert any("a.py" in l for l in lines)
        assert not any("b.js" in l for l in lines)
        assert not any("doc.md" in l for l in lines)

    def test_find_by_type(self, tmp_path: Path) -> None:
        """按文件类型简写过滤。"""
        _make_tree(tmp_path, {
            "a.py": "pass",
            "b.md": "# doc",
            "c.json": "{}",
        })

        ctx = _make_ctx(str(tmp_path))
        tool = FindFilesTool.create(ctx)

        result = _run(tool.execute(path=str(tmp_path), type="md"))
        lines = str(result).split("\n")

        assert any("b.md" in l for l in lines)
        assert not any("a.py" in l for l in lines)
        assert not any("c.json" in l for l in lines)

    def test_find_ignore_dirs(self, tmp_path: Path) -> None:
        """噪声目录被过滤。"""
        _make_tree(tmp_path, {
            "src": {"main.py": "pass"},
            ".git": {"config": "[core]"},
            "node_modules": {"pkg": {"index.js": "// js"}},
            "__pycache__": {"main.cpython.pyc": "binary"},
        })

        ctx = _make_ctx(str(tmp_path))
        tool = FindFilesTool.create(ctx)

        result = _run(tool.execute(path=str(tmp_path)))
        text = str(result)

        assert "main.py" in text
        assert ".git" not in text
        assert "node_modules" not in text
        assert "__pycache__" not in text

    def test_find_head_limit(self, tmp_path: Path) -> None:
        """超过 head_limit 时截断并提示。"""
        for i in range(10):
            (tmp_path / f"file_{i:02d}.txt").write_text("x")

        ctx = _make_ctx(str(tmp_path))
        tool = FindFilesTool.create(ctx)

        result = _run(tool.execute(path=str(tmp_path), head_limit=3))
        text = str(result)

        assert "truncated" in text.lower()
        assert "showing first 3 of 10" in text

    def test_find_no_results(self, tmp_path: Path) -> None:
        """无匹配返回提示。"""
        (tmp_path / "a.py").write_text("pass")

        ctx = _make_ctx(str(tmp_path))
        tool = FindFilesTool.create(ctx)

        result = _run(tool.execute(path=str(tmp_path), glob="*.xyz"))
        assert "No files found" in str(result)

    def test_find_path_not_found(self, tmp_path: Path) -> None:
        """路径不存在返回错误。"""
        ctx = _make_ctx(str(tmp_path))
        tool = FindFilesTool.create(ctx)

        result = _run(tool.execute(path=str(tmp_path / "nonexistent")))
        assert isinstance(result, ToolResult)
        assert result.is_error
        assert "Path not found" in str(result)

    def test_find_single_file(self, tmp_path: Path) -> None:
        """搜索单个文件时返回该文件。"""
        target = tmp_path / "single.py"
        target.write_text("pass")

        ctx = _make_ctx(str(tmp_path))
        tool = FindFilesTool.create(ctx)

        result = _run(tool.execute(path=str(target)))
        assert "single.py" in str(result)


# ---------------------------------------------------------------------------
# GrepTool
# ---------------------------------------------------------------------------


class TestGrepTool:
    """GrepTool 内容搜索。"""

    def test_grep_content_mode(self, tmp_path: Path) -> None:
        """content 模式返回匹配行+行号。"""
        (tmp_path / "test.py").write_text("line1\nhello world\nline3\nhello again\n")

        ctx = _make_ctx(str(tmp_path))
        tool = GrepTool.create(ctx)

        result = _run(tool.execute(pattern="hello", path=str(tmp_path), output_mode="content"))
        lines = str(result).split("\n")

        assert any("test.py:2|" in l for l in lines)
        assert any("test.py:4|" in l for l in lines)
        assert any("hello world" in l for l in lines)

    def test_grep_files_with_matches(self, tmp_path: Path) -> None:
        """files_with_matches 模式返回匹配文件路径。"""
        (tmp_path / "a.py").write_text("hello\n")
        (tmp_path / "b.py").write_text("world\n")
        (tmp_path / "c.py").write_text("hello again\n")

        ctx = _make_ctx(str(tmp_path))
        tool = GrepTool.create(ctx)

        result = _run(tool.execute(pattern="hello", path=str(tmp_path)))
        lines = str(result).split("\n")

        assert any("a.py" in l for l in lines)
        assert any("c.py" in l for l in lines)
        assert not any("b.py" in l for l in lines)

    def test_grep_case_insensitive(self, tmp_path: Path) -> None:
        """不区分大小写搜索。"""
        (tmp_path / "test.py").write_text("Hello World\nhello world\n")

        ctx = _make_ctx(str(tmp_path))
        tool = GrepTool.create(ctx)

        result = _run(tool.execute(
            pattern="HELLO", path=str(tmp_path),
            case_insensitive=True, output_mode="content",
        ))
        text = str(result)

        assert "Hello World" in text
        assert "hello world" in text

    def test_grep_fixed_strings(self, tmp_path: Path) -> None:
        """固定字符串模式（非正则）。"""
        (tmp_path / "test.py").write_text("a.b.c\naXbXc\n")

        ctx = _make_ctx(str(tmp_path))
        tool = GrepTool.create(ctx)

        # fixed_strings=True 时 . 是字面量，只匹配第一行
        result = _run(tool.execute(
            pattern="a.b", path=str(tmp_path),
            fixed_strings=True, output_mode="content",
        ))
        text = str(result)

        assert "a.b.c" in text
        assert "aXbXc" not in text

    def test_grep_regex(self, tmp_path: Path) -> None:
        """正则表达式模式。"""
        (tmp_path / "test.py").write_text("foo123\nbar456\nbaz\n")

        ctx = _make_ctx(str(tmp_path))
        tool = GrepTool.create(ctx)

        result = _run(tool.execute(
            pattern=r"\d+", path=str(tmp_path), output_mode="content",
        ))
        text = str(result)

        assert "foo123" in text
        assert "bar456" in text
        assert "baz" not in text

    def test_grep_glob_filter(self, tmp_path: Path) -> None:
        """glob 过滤搜索文件。"""
        (tmp_path / "a.py").write_text("hello\n")
        (tmp_path / "b.txt").write_text("hello\n")

        ctx = _make_ctx(str(tmp_path))
        tool = GrepTool.create(ctx)

        result = _run(tool.execute(pattern="hello", path=str(tmp_path), glob="*.py"))
        text = str(result)

        assert "a.py" in text
        assert "b.txt" not in text

    def test_grep_skip_binary(self, tmp_path: Path) -> None:
        """二进制文件被跳过。"""
        (tmp_path / "text.txt").write_text("hello\n")
        (tmp_path / "binary.bin").write_bytes(b"hello\x00world\n")

        ctx = _make_ctx(str(tmp_path))
        tool = GrepTool.create(ctx)

        result = _run(tool.execute(pattern="hello", path=str(tmp_path)))
        text = str(result)

        assert "text.txt" in text
        assert "binary.bin" not in text

    def test_grep_invalid_regex(self, tmp_path: Path) -> None:
        """无效正则返回错误。"""
        (tmp_path / "test.py").write_text("hello\n")

        ctx = _make_ctx(str(tmp_path))
        tool = GrepTool.create(ctx)

        result = _run(tool.execute(pattern="[invalid", path=str(tmp_path)))
        assert isinstance(result, ToolResult)
        assert result.is_error
        assert "invalid regex" in str(result).lower()

    def test_grep_no_matches(self, tmp_path: Path) -> None:
        """无匹配返回提示。"""
        (tmp_path / "test.py").write_text("hello\n")

        ctx = _make_ctx(str(tmp_path))
        tool = GrepTool.create(ctx)

        result = _run(tool.execute(pattern="xyz", path=str(tmp_path)))
        assert "No matches found" in str(result)

    def test_grep_empty_pattern(self, tmp_path: Path) -> None:
        """空 pattern 返回错误。"""
        ctx = _make_ctx(str(tmp_path))
        tool = GrepTool.create(ctx)

        result = _run(tool.execute(pattern="", path=str(tmp_path)))
        assert isinstance(result, ToolResult)
        assert result.is_error

    def test_grep_path_not_found(self, tmp_path: Path) -> None:
        """路径不存在返回错误。"""
        ctx = _make_ctx(str(tmp_path))
        tool = GrepTool.create(ctx)

        result = _run(tool.execute(pattern="test", path=str(tmp_path / "nonexistent")))
        assert isinstance(result, ToolResult)
        assert result.is_error


# ---------------------------------------------------------------------------
# 工具发现
# ---------------------------------------------------------------------------


class TestSearchToolDiscovery:
    """搜索工具被 ToolLoader 自动发现。"""

    def test_both_discovered(self, tmp_path: Path) -> None:
        """find_files 和 grep 都被发现并注册。"""
        ctx = _make_ctx(str(tmp_path))
        registry = ToolRegistry()
        loaded = ToolLoader().load(ctx, registry, scope="core")

        assert "find_files" in loaded
        assert "grep" in loaded
        assert registry.has("find_files")
        assert registry.has("grep")

    def test_find_files_schema(self, tmp_path: Path) -> None:
        """FindFilesTool 参数 schema 正确。"""
        ctx = _make_ctx(str(tmp_path))
        tool = FindFilesTool.create(ctx)
        schema = tool.to_schema()

        assert schema["function"]["name"] == "find_files"
        props = schema["function"]["parameters"]["properties"]
        assert "path" in props
        assert "query" in props
        assert "glob" in props
        assert "type" in props
        assert "head_limit" in props

    def test_grep_schema(self, tmp_path: Path) -> None:
        """GrepTool 参数 schema 正确。"""
        ctx = _make_ctx(str(tmp_path))
        tool = GrepTool.create(ctx)
        schema = tool.to_schema()

        assert schema["function"]["name"] == "grep"
        props = schema["function"]["parameters"]["properties"]
        assert "pattern" in props
        assert "path" in props
        assert "glob" in props
        assert "output_mode" in props
        assert "pattern" in schema["function"]["parameters"]["required"]

    def test_both_read_only(self, tmp_path: Path) -> None:
        """两个搜索工具都是只读的。"""
        ctx = _make_ctx(str(tmp_path))
        assert FindFilesTool.create(ctx).read_only is True
        assert GrepTool.create(ctx).read_only is True
