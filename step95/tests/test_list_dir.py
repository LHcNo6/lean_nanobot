"""step67：ListDirTool 单元测试。

覆盖：
- 非递归列表
- 递归列表
- 噪声目录过滤
- max_entries 截断
- 空目录
- 目录不存在/不是目录
- 目录后缀标识
- 只读属性
- 工具发现与 schema
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from step95.context import ToolContext
from step95.loader import ToolLoader
from step95.tool import ToolRegistry, ToolResult
from step95.tools.file_state import FileStateStore
from step95.tools.filesystem import ListDirTool


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _make_config(*, restrict: bool = False) -> SimpleNamespace:
    """构造最小化的 mock config。"""
    return SimpleNamespace(
        tools=SimpleNamespace(
            restrict_to_workspace=restrict,
            file=SimpleNamespace(enable=True),
        )
    )


def _make_ctx(workspace: str, *, restrict: bool = False) -> ToolContext:
    """构造测试用 ToolContext。"""
    return ToolContext(
        config=_make_config(restrict=restrict),
        workspace=workspace,
        restrict_to_workspace=restrict,
        file_state_store=FileStateStore(),
        session_key="test-session",
    )


def _run(coro):
    """同步执行协程。"""
    return asyncio.run(coro)


def _make_tree(root: Path, structure: dict) -> None:
    """在 root 下创建目录树结构。

    structure: {"dir1": {"file1.txt": "content", "subdir": {...}}, "file2.txt": "content"}
    """
    for name, value in structure.items():
        path = root / name
        if isinstance(value, dict):
            path.mkdir(parents=True, exist_ok=True)
            _make_tree(path, value)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(value), encoding="utf-8")


# ---------------------------------------------------------------------------
# ListDirTool 基础功能
# ---------------------------------------------------------------------------


class TestListDirToolBasic:
    """ListDirTool 基础列表功能。"""

    def test_list_non_recursive(self, tmp_path: Path) -> None:
        """非递归模式列出直接子项。"""
        _make_tree(tmp_path, {
            "src": {"main.py": "print('hello')"},
            "tests": {"test_main.py": "pass"},
            "README.md": "# Test",
        })

        ctx = _make_ctx(str(tmp_path))
        tool = ListDirTool.create(ctx)

        result = _run(tool.execute(path=str(tmp_path)))

        assert not (isinstance(result, ToolResult) and result.is_error)
        lines = str(result).split("\n")
        assert "src/" in lines
        assert "tests/" in lines
        assert "README.md" in lines
        # 非递归不包含深层文件
        assert "main.py" not in lines

    def test_list_recursive(self, tmp_path: Path) -> None:
        """递归模式遍历所有子项。"""
        _make_tree(tmp_path, {
            "src": {"main.py": "print('hello')", "utils.py": "pass"},
            "README.md": "# Test",
        })

        ctx = _make_ctx(str(tmp_path))
        tool = ListDirTool.create(ctx)

        result = _run(tool.execute(path=str(tmp_path), recursive=True))

        lines = str(result).split("\n")
        assert "src/" in lines
        assert "src/main.py" in lines
        assert "src/utils.py" in lines
        assert "README.md" in lines

    def test_dir_suffix(self, tmp_path: Path) -> None:
        """目录项带 / 后缀，文件项不带。"""
        (tmp_path / "mydir").mkdir()
        (tmp_path / "myfile.txt").write_text("content")

        ctx = _make_ctx(str(tmp_path))
        tool = ListDirTool.create(ctx)

        result = _run(tool.execute(path=str(tmp_path)))
        lines = str(result).split("\n")

        assert "mydir/" in lines
        assert "myfile.txt" in lines

    def test_empty_directory(self, tmp_path: Path) -> None:
        """空目录返回空目录消息。"""
        ctx = _make_ctx(str(tmp_path))
        tool = ListDirTool.create(ctx)

        result = _run(tool.execute(path=str(tmp_path)))

        assert "empty" in str(result).lower()


# ---------------------------------------------------------------------------
# 噪声目录过滤
# ---------------------------------------------------------------------------


class TestListDirToolIgnore:
    """ListDirTool 噪声目录过滤。"""

    def test_ignore_git_non_recursive(self, tmp_path: Path) -> None:
        """非递归模式过滤 .git 等噪声目录。"""
        (tmp_path / ".git").mkdir()
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "__pycache__").mkdir()
        (tmp_path / "src").mkdir()
        (tmp_path / "README.md").write_text("hi")

        ctx = _make_ctx(str(tmp_path))
        tool = ListDirTool.create(ctx)

        result = _run(tool.execute(path=str(tmp_path)))
        lines = str(result).split("\n")

        assert ".git/" not in lines
        assert "node_modules/" not in lines
        assert "__pycache__/" not in lines
        assert "src/" in lines
        assert "README.md" in lines

    def test_ignore_git_recursive(self, tmp_path: Path) -> None:
        """递归模式过滤路径中包含噪声目录的项。"""
        _make_tree(tmp_path, {
            "src": {"main.py": "pass"},
            ".git": {"config": "[core]", "objects": {"pack": {"file": "data"}}},
            "node_modules": {"pkg": {"index.js": "// js"}},
        })

        ctx = _make_ctx(str(tmp_path))
        tool = ListDirTool.create(ctx)

        result = _run(tool.execute(path=str(tmp_path), recursive=True))
        lines = str(result).split("\n")

        assert "src/" in lines
        assert "src/main.py" in lines
        # .git 及其子项全部过滤
        assert not any(line.startswith(".git") for line in lines)
        assert not any(line.startswith("node_modules") for line in lines)


# ---------------------------------------------------------------------------
# 截断
# ---------------------------------------------------------------------------


class TestListDirToolTruncation:
    """ListDirTool max_entries 截断。"""

    def test_max_entries_truncation(self, tmp_path: Path) -> None:
        """超过 max_entries 时截断并提示。"""
        for i in range(10):
            (tmp_path / f"file_{i:02d}.txt").write_text(f"content {i}")

        ctx = _make_ctx(str(tmp_path))
        tool = ListDirTool.create(ctx)

        result = _run(tool.execute(path=str(tmp_path), max_entries=5))
        text = str(result)

        assert "truncated" in text.lower()
        assert "showing first 5 of 10" in text
        # 只返回前 5 个文件 + 截断提示行
        lines = text.split("\n")
        file_lines = [l for l in lines if l.endswith(".txt")]
        assert len(file_lines) == 5

    def test_custom_max_entries(self, tmp_path: Path) -> None:
        """自定义 max_entries 生效。"""
        for i in range(5):
            (tmp_path / f"f{i}.txt").write_text("x")

        ctx = _make_ctx(str(tmp_path))
        tool = ListDirTool.create(ctx)

        result = _run(tool.execute(path=str(tmp_path), max_entries=3))
        text = str(result)

        assert "showing first 3 of 5" in text

    def test_below_max_no_truncation(self, tmp_path: Path) -> None:
        """条目数少于 max_entries 时不截断。"""
        for i in range(3):
            (tmp_path / f"f{i}.txt").write_text("x")

        ctx = _make_ctx(str(tmp_path))
        tool = ListDirTool.create(ctx)

        result = _run(tool.execute(path=str(tmp_path), max_entries=10))
        text = str(result)

        assert "truncated" not in text.lower()


# ---------------------------------------------------------------------------
# 错误处理
# ---------------------------------------------------------------------------


class TestListDirToolErrors:
    """ListDirTool 错误处理。"""

    def test_directory_not_found(self, tmp_path: Path) -> None:
        """目录不存在返回错误。"""
        ctx = _make_ctx(str(tmp_path))
        tool = ListDirTool.create(ctx)

        result = _run(tool.execute(path=str(tmp_path / "nonexistent")))

        assert isinstance(result, ToolResult)
        assert result.is_error
        assert "Directory not found" in str(result)

    def test_not_a_directory(self, tmp_path: Path) -> None:
        """路径是文件返回错误。"""
        target = tmp_path / "file.txt"
        target.write_text("content")

        ctx = _make_ctx(str(tmp_path))
        tool = ListDirTool.create(ctx)

        result = _run(tool.execute(path=str(target)))

        assert isinstance(result, ToolResult)
        assert result.is_error
        assert "Not a directory" in str(result)

    def test_empty_path(self, tmp_path: Path) -> None:
        """空路径返回错误。"""
        ctx = _make_ctx(str(tmp_path))
        tool = ListDirTool.create(ctx)

        result = _run(tool.execute(path=""))

        assert isinstance(result, ToolResult)
        assert result.is_error
        assert "path" in str(result).lower()


# ---------------------------------------------------------------------------
# 工具发现与注册
# ---------------------------------------------------------------------------


class TestListDirToolDiscovery:
    """ListDirTool 被 ToolLoader 自动发现。"""

    def test_discovered_by_loader(self, tmp_path: Path) -> None:
        """ToolLoader 能发现并注册 ListDirTool。"""
        ctx = _make_ctx(str(tmp_path))
        registry = ToolRegistry()
        loaded = ToolLoader().load(ctx, registry, scope="core")

        assert "list_dir" in loaded
        assert registry.has("list_dir")

    def test_tool_schema(self, tmp_path: Path) -> None:
        """ListDirTool 的 to_schema 符合 OpenAI function 格式。"""
        ctx = _make_ctx(str(tmp_path))
        tool = ListDirTool.create(ctx)
        schema = tool.to_schema()

        assert schema["type"] == "function"
        assert schema["function"]["name"] == "list_dir"
        props = schema["function"]["parameters"]["properties"]
        assert "path" in props
        assert "recursive" in props
        assert "max_entries" in props
        assert "path" in schema["function"]["parameters"]["required"]

    def test_read_only(self, tmp_path: Path) -> None:
        """ListDirTool 是只读工具。"""
        ctx = _make_ctx(str(tmp_path))
        tool = ListDirTool.create(ctx)
        assert tool.read_only is True
