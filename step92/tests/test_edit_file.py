"""step66：EditFileTool 单元测试。

覆盖：
- 单匹配替换
- replace_all
- occurrence 选择
- 多匹配歧义
- old_text 未找到
- 文件不存在
- read-before-edit 警告
- CRLF 保留
- 参数互斥/越界
- 空 old_text
- 工具发现与 schema
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from step92.context import ToolContext
from step92.loader import ToolLoader
from step92.tool import ToolRegistry, ToolResult
from step92.tools.file_state import FileStateStore, FileStates
from step92.tools.filesystem import EditFileTool, _find_matches, _MatchSpan


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


def _make_ctx(
    workspace: str,
    *,
    restrict: bool = False,
    file_state_store: FileStateStore | None = None,
    session_key: str = "test-session",
) -> ToolContext:
    """构造测试用 ToolContext。"""
    return ToolContext(
        config=_make_config(restrict=restrict),
        workspace=workspace,
        restrict_to_workspace=restrict,
        file_state_store=file_state_store or FileStateStore(),
        session_key=session_key,
    )


def _run(coro):
    """同步执行协程。"""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# _find_matches 辅助函数
# ---------------------------------------------------------------------------


class TestFindMatches:
    """_find_matches 精确匹配查找。"""

    def test_single_match(self) -> None:
        """单个匹配返回正确位置。"""
        matches = _find_matches("hello world", "world")
        assert len(matches) == 1
        assert matches[0].start == 6
        assert matches[0].end == 11
        assert matches[0].text == "world"
        assert matches[0].line == 1

    def test_multiple_matches(self) -> None:
        """多个匹配返回所有位置。"""
        matches = _find_matches("a b a b a", "a")
        assert len(matches) == 3
        assert [m.start for m in matches] == [0, 4, 8]

    def test_no_match(self) -> None:
        """无匹配返回空列表。"""
        assert _find_matches("hello", "xyz") == []

    def test_empty_old_text(self) -> None:
        """空 old_text 返回空列表（避免无限循环）。"""
        assert _find_matches("hello", "") == []

    def test_multiline_line_numbers(self) -> None:
        """多行文本的行号正确（1-indexed）。"""
        content = "line1\nline2\nline3"
        matches = _find_matches(content, "line")
        assert len(matches) == 3
        assert [m.line for m in matches] == [1, 2, 3]

    def test_overlapping_not_counted(self) -> None:
        """重叠匹配不重复计数（步进 max(1, len)）。"""
        matches = _find_matches("aaa", "aa")
        assert len(matches) == 1  # 只匹配第一个 "aa"，不重叠匹配第二个


# ---------------------------------------------------------------------------
# EditFileTool 基础功能
# ---------------------------------------------------------------------------


class TestEditFileToolBasic:
    """EditFileTool 基础替换功能。"""

    def test_single_match_replace(self, tmp_path: Path) -> None:
        """单匹配替换成功，内容正确。"""
        target = tmp_path / "test.txt"
        target.write_text("hello world", encoding="utf-8")

        ctx = _make_ctx(str(tmp_path))
        tool = EditFileTool.create(ctx)

        result = _run(tool.execute(path=str(target), old_text="world", new_text="there"))

        assert not (isinstance(result, ToolResult) and result.is_error)
        assert target.read_text(encoding="utf-8") == "hello there"
        assert "Successfully edited" in str(result)

    def test_replace_all(self, tmp_path: Path) -> None:
        """replace_all=True 替换所有匹配。"""
        target = tmp_path / "test.txt"
        target.write_text("a b a b a", encoding="utf-8")

        ctx = _make_ctx(str(tmp_path))
        tool = EditFileTool.create(ctx)

        result = _run(tool.execute(path=str(target), old_text="a", new_text="x", replace_all=True))

        assert not (isinstance(result, ToolResult) and result.is_error)
        assert target.read_text(encoding="utf-8") == "x b x b x"

    def test_occurrence_select(self, tmp_path: Path) -> None:
        """occurrence=N 替换第 N 个匹配。"""
        target = tmp_path / "test.txt"
        target.write_text("a b a b a", encoding="utf-8")

        ctx = _make_ctx(str(tmp_path))
        tool = EditFileTool.create(ctx)

        result = _run(tool.execute(path=str(target), old_text="a", new_text="x", occurrence=2))

        assert not (isinstance(result, ToolResult) and result.is_error)
        assert target.read_text(encoding="utf-8") == "a b x b a"

    def test_replace_with_empty_new_text(self, tmp_path: Path) -> None:
        """new_text 为空字符串时删除匹配文本。"""
        target = tmp_path / "test.txt"
        target.write_text("hello world", encoding="utf-8")

        ctx = _make_ctx(str(tmp_path))
        tool = EditFileTool.create(ctx)

        result = _run(tool.execute(path=str(target), old_text=" world", new_text=""))

        assert not (isinstance(result, ToolResult) and result.is_error)
        assert target.read_text(encoding="utf-8") == "hello"

    def test_multiline_replace(self, tmp_path: Path) -> None:
        """多行文本替换成功。"""
        target = tmp_path / "test.txt"
        target.write_text("line1\nline2\nline3", encoding="utf-8")

        ctx = _make_ctx(str(tmp_path))
        tool = EditFileTool.create(ctx)

        result = _run(tool.execute(path=str(target), old_text="line2", new_text="LINE2"))

        assert not (isinstance(result, ToolResult) and result.is_error)
        assert target.read_text(encoding="utf-8") == "line1\nLINE2\nline3"


# ---------------------------------------------------------------------------
# 多匹配歧义处理
# ---------------------------------------------------------------------------


class TestEditFileToolAmbiguity:
    """EditFileTool 多匹配歧义处理。"""

    def test_multiple_matches_ambiguous(self, tmp_path: Path) -> None:
        """多匹配且无参数时返回警告，不执行替换。"""
        target = tmp_path / "test.txt"
        target.write_text("a b a b a", encoding="utf-8")

        ctx = _make_ctx(str(tmp_path))
        tool = EditFileTool.create(ctx)

        result = _run(tool.execute(path=str(target), old_text="a", new_text="x"))

        # 返回的是警告字符串（非 ToolResult.error），文件未被修改
        assert isinstance(result, str)
        assert "Warning" in result
        assert "appears 3 times" in result
        assert target.read_text(encoding="utf-8") == "a b a b a"

    def test_ambiguous_warning_lists_lines(self, tmp_path: Path) -> None:
        """歧义警告列出匹配行号。"""
        target = tmp_path / "test.txt"
        target.write_text("foo\nbar\nfoo\nbaz\nfoo", encoding="utf-8")

        ctx = _make_ctx(str(tmp_path))
        tool = EditFileTool.create(ctx)

        result = _run(tool.execute(path=str(target), old_text="foo", new_text="x"))

        assert "line 1" in str(result)
        assert "line 3" in str(result)
        assert "line 5" in str(result)


# ---------------------------------------------------------------------------
# 错误处理
# ---------------------------------------------------------------------------


class TestEditFileToolErrors:
    """EditFileTool 错误处理。"""

    def test_old_text_not_found(self, tmp_path: Path) -> None:
        """old_text 未找到返回错误。"""
        target = tmp_path / "test.txt"
        target.write_text("hello world", encoding="utf-8")

        ctx = _make_ctx(str(tmp_path))
        tool = EditFileTool.create(ctx)

        result = _run(tool.execute(path=str(target), old_text="xyz", new_text="abc"))

        assert isinstance(result, ToolResult)
        assert result.is_error
        assert "old_text not found" in str(result)

    def test_file_not_found(self, tmp_path: Path) -> None:
        """文件不存在返回错误。"""
        ctx = _make_ctx(str(tmp_path))
        tool = EditFileTool.create(ctx)

        result = _run(tool.execute(path=str(tmp_path / "nonexistent.txt"), old_text="a", new_text="b"))

        assert isinstance(result, ToolResult)
        assert result.is_error
        assert "File not found" in str(result)

    def test_empty_path(self, tmp_path: Path) -> None:
        """空路径返回错误。"""
        ctx = _make_ctx(str(tmp_path))
        tool = EditFileTool.create(ctx)

        result = _run(tool.execute(path="", old_text="a", new_text="b"))

        assert isinstance(result, ToolResult)
        assert result.is_error
        assert "path" in str(result).lower()

    def test_replace_all_and_occurrence_mutually_exclusive(self, tmp_path: Path) -> None:
        """replace_all 与 occurrence 互斥。"""
        target = tmp_path / "test.txt"
        target.write_text("a a a", encoding="utf-8")

        ctx = _make_ctx(str(tmp_path))
        tool = EditFileTool.create(ctx)

        result = _run(tool.execute(path=str(target), old_text="a", new_text="x", replace_all=True, occurrence=1))

        assert isinstance(result, ToolResult)
        assert result.is_error
        assert "cannot be used with replace_all" in str(result)

    def test_occurrence_out_of_range(self, tmp_path: Path) -> None:
        """occurrence 越界返回错误。"""
        target = tmp_path / "test.txt"
        target.write_text("a a", encoding="utf-8")

        ctx = _make_ctx(str(tmp_path))
        tool = EditFileTool.create(ctx)

        result = _run(tool.execute(path=str(target), old_text="a", new_text="x", occurrence=5))

        assert isinstance(result, ToolResult)
        assert result.is_error
        assert "out of range" in str(result)

    def test_empty_old_text(self, tmp_path: Path) -> None:
        """空 old_text 视为未找到匹配。"""
        target = tmp_path / "test.txt"
        target.write_text("hello", encoding="utf-8")

        ctx = _make_ctx(str(tmp_path))
        tool = EditFileTool.create(ctx)

        result = _run(tool.execute(path=str(target), old_text="", new_text="x"))

        assert isinstance(result, ToolResult)
        assert result.is_error
        assert "old_text not found" in str(result)


# ---------------------------------------------------------------------------
# read-before-edit 警告
# ---------------------------------------------------------------------------


class TestEditFileToolReadBeforeEdit:
    """EditFileTool read-before-edit 警告。"""

    def test_warning_when_not_read(self, tmp_path: Path) -> None:
        """文件未读取过时编辑返回警告。"""
        target = tmp_path / "test.txt"
        target.write_text("hello world", encoding="utf-8")

        store = FileStateStore()
        ctx = _make_ctx(str(tmp_path), file_state_store=store)
        tool = EditFileTool.create(ctx)

        result = _run(tool.execute(path=str(target), old_text="world", new_text="there"))

        assert "Warning" in str(result)
        assert "not been read" in str(result)
        # 替换仍然执行
        assert target.read_text(encoding="utf-8") == "hello there"

    def test_no_warning_when_read(self, tmp_path: Path) -> None:
        """文件已读取过时编辑无警告。"""
        target = tmp_path / "test.txt"
        target.write_text("hello world", encoding="utf-8")

        store = FileStateStore()
        states = store.for_session("test-session")
        states.record_read(target)

        ctx = _make_ctx(str(tmp_path), file_state_store=store)
        tool = EditFileTool.create(ctx)

        result = _run(tool.execute(path=str(target), old_text="world", new_text="there"))

        assert "Warning" not in str(result)
        assert target.read_text(encoding="utf-8") == "hello there"


# ---------------------------------------------------------------------------
# CRLF 保留
# ---------------------------------------------------------------------------


class TestEditFileToolCRLF:
    """EditFileTool CRLF 换行符保留。"""

    def test_crlf_preserved(self, tmp_path: Path) -> None:
        """CRLF 文件编辑后保持 CRLF。"""
        target = tmp_path / "test.txt"
        target.write_bytes(b"line1\r\nline2\r\nline3\r\n")

        ctx = _make_ctx(str(tmp_path))
        tool = EditFileTool.create(ctx)

        result = _run(tool.execute(path=str(target), old_text="line2", new_text="LINE2"))

        assert not (isinstance(result, ToolResult) and result.is_error)
        raw = target.read_bytes()
        assert b"\r\n" in raw
        assert raw == b"line1\r\nLINE2\r\nline3\r\n"

    def test_lf_preserved(self, tmp_path: Path) -> None:
        """LF 文件编辑后保持 LF。"""
        target = tmp_path / "test.txt"
        target.write_bytes(b"line1\nline2\nline3\n")

        ctx = _make_ctx(str(tmp_path))
        tool = EditFileTool.create(ctx)

        result = _run(tool.execute(path=str(target), old_text="line2", new_text="LINE2"))

        assert not (isinstance(result, ToolResult) and result.is_error)
        raw = target.read_bytes()
        assert b"\r\n" not in raw
        assert raw == b"line1\nLINE2\nline3\n"


# ---------------------------------------------------------------------------
# 工具发现与注册
# ---------------------------------------------------------------------------


class TestEditFileToolDiscovery:
    """EditFileTool 被 ToolLoader 自动发现。"""

    def test_discovered_by_loader(self, tmp_path: Path) -> None:
        """ToolLoader 能发现并注册 EditFileTool。"""
        ctx = _make_ctx(str(tmp_path))
        registry = ToolRegistry()
        loaded = ToolLoader().load(ctx, registry, scope="core")

        assert "edit_file" in loaded
        assert registry.has("edit_file")

    def test_tool_schema(self, tmp_path: Path) -> None:
        """EditFileTool 的 to_schema 符合 OpenAI function 格式。"""
        ctx = _make_ctx(str(tmp_path))
        tool = EditFileTool.create(ctx)
        schema = tool.to_schema()

        assert schema["type"] == "function"
        assert schema["function"]["name"] == "edit_file"
        props = schema["function"]["parameters"]["properties"]
        assert "path" in props
        assert "old_text" in props
        assert "new_text" in props
        assert "replace_all" in props
        assert "occurrence" in props
        required = schema["function"]["parameters"]["required"]
        assert "path" in required
        assert "old_text" in required
        assert "new_text" in required

    def test_not_read_only(self, tmp_path: Path) -> None:
        """EditFileTool 非只读。"""
        ctx = _make_ctx(str(tmp_path))
        tool = EditFileTool.create(ctx)
        assert tool.read_only is False
