"""step65：WriteFileTool 单元测试。

覆盖：
- 写入新文件
- 覆盖已有文件
- 自动创建父目录
- 文件状态记录（record_write）
- 受限模式边界守卫
- 空参数错误处理
- ToolLoader 自动发现
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from step80.context import ToolContext
from step80.loader import ToolLoader
from step80.security.workspace_policy import WorkspaceBoundaryError
from step80.tool import ToolRegistry, ToolResult
from step80.tools.file_state import FileStateStore, FileStates
from step80.tools.filesystem import WriteFileTool, _FsTool


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _make_config(*, restrict: bool = False, file_enable: bool = True) -> SimpleNamespace:
    """构造最小化的 mock config（避免触发 Config() 的 openai 依赖）。"""
    tools = SimpleNamespace(
        restrict_to_workspace=restrict,
        file=SimpleNamespace(enable=file_enable),
    )
    return SimpleNamespace(tools=tools)


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
    """同步执行协程（测试辅助）。"""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# WriteFileTool 基础功能
# ---------------------------------------------------------------------------


class TestWriteFileToolBasic:
    """WriteFileTool 基础写入功能。"""

    def test_write_new_file(self, tmp_path: Path) -> None:
        """写入新文件成功，内容正确，返回消息包含字符数。"""
        ctx = _make_ctx(str(tmp_path))
        tool = WriteFileTool.create(ctx)

        target = tmp_path / "hello.txt"
        result = _run(tool.execute(path=str(target), content="Hello, World!"))

        assert not isinstance(result, ToolResult) or not result.is_error
        assert target.exists()
        assert target.read_text(encoding="utf-8") == "Hello, World!"
        assert "13 characters" in str(result)  # len("Hello, World!") == 13

    def test_overwrite_existing_file(self, tmp_path: Path) -> None:
        """覆盖已有文件成功，旧内容被替换。"""
        ctx = _make_ctx(str(tmp_path))
        tool = WriteFileTool.create(ctx)

        target = tmp_path / "existing.txt"
        target.write_text("old content", encoding="utf-8")

        result = _run(tool.execute(path=str(target), content="new content"))

        assert not (isinstance(result, ToolResult) and result.is_error)
        assert target.read_text(encoding="utf-8") == "new content"

    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        """父目录不存在时自动创建。"""
        ctx = _make_ctx(str(tmp_path))
        tool = WriteFileTool.create(ctx)

        target = tmp_path / "a" / "b" / "c" / "deep.txt"
        result = _run(tool.execute(path=str(target), content="deep"))

        assert not (isinstance(result, ToolResult) and result.is_error)
        assert target.exists()
        assert target.read_text(encoding="utf-8") == "deep"

    def test_write_empty_content(self, tmp_path: Path) -> None:
        """写入空字符串成功（创建空文件）。"""
        ctx = _make_ctx(str(tmp_path))
        tool = WriteFileTool.create(ctx)

        target = tmp_path / "empty.txt"
        result = _run(tool.execute(path=str(target), content=""))

        assert not (isinstance(result, ToolResult) and result.is_error)
        assert target.exists()
        assert target.read_text(encoding="utf-8") == ""
        assert "0 characters" in str(result)

    def test_utf8_content(self, tmp_path: Path) -> None:
        """写入 UTF-8 中文内容正确。"""
        ctx = _make_ctx(str(tmp_path))
        tool = WriteFileTool.create(ctx)

        target = tmp_path / "chinese.txt"
        content = "你好，世界！🚀"
        result = _run(tool.execute(path=str(target), content=content))

        assert not (isinstance(result, ToolResult) and result.is_error)
        assert target.read_text(encoding="utf-8") == content


# ---------------------------------------------------------------------------
# 文件状态追踪
# ---------------------------------------------------------------------------


class TestWriteFileToolState:
    """WriteFileTool 与 FileStates 集成。"""

    def test_record_write_marks_not_dedupable(self, tmp_path: Path) -> None:
        """写入后 FileStates 标记文件为不可 dedup。"""
        store = FileStateStore()
        ctx = _make_ctx(str(tmp_path), file_state_store=store)
        tool = WriteFileTool.create(ctx)

        target = tmp_path / "stateful.txt"
        _run(tool.execute(path=str(target), content="v1"))

        states = store.for_session("test-session")
        entry = states.get(target)
        assert entry is not None
        assert entry.can_dedup is False  # 写入后不可 dedup

    def test_write_after_read_invalidates_dedup(self, tmp_path: Path) -> None:
        """先读后写，写入后原 read 状态失效。"""
        store = FileStateStore()
        ctx = _make_ctx(str(tmp_path), file_state_store=store)
        states = store.for_session("test-session")

        target = tmp_path / "read_then_write.txt"
        target.write_text("original", encoding="utf-8")
        states.record_read(target)

        # 确认读取后可以 dedup
        assert states.is_unchanged(target) is True

        # 写入覆盖
        tool = WriteFileTool.create(ctx)
        _run(tool.execute(path=str(target), content="modified"))

        # 写入后不可 dedup
        entry = states.get(target)
        assert entry is not None
        assert entry.can_dedup is False


# ---------------------------------------------------------------------------
# 边界守卫
# ---------------------------------------------------------------------------


class TestWriteFileToolBoundary:
    """WriteFileTool workspace 边界守卫。"""

    def test_restricted_write_inside_workspace(self, tmp_path: Path) -> None:
        """受限模式下写入 workspace 内路径成功。"""
        ctx = _make_ctx(str(tmp_path), restrict=True)
        tool = WriteFileTool.create(ctx)

        target = tmp_path / "inside.txt"
        result = _run(tool.execute(path=str(target), content="inside"))

        assert not (isinstance(result, ToolResult) and result.is_error)
        assert target.exists()

    def test_restricted_write_outside_workspace(self, tmp_path: Path) -> None:
        """受限模式下写入 workspace 外路径返回错误。"""
        outside = tmp_path.parent / "outside_workspace"
        outside.mkdir(exist_ok=True)
        ctx = _make_ctx(str(tmp_path), restrict=True)
        tool = WriteFileTool.create(ctx)

        target = outside / "evil.txt"
        result = _run(tool.execute(path=str(target), content="evil"))

        assert isinstance(result, ToolResult)
        assert result.is_error
        assert not target.exists()

    def test_unrestricted_write_anywhere(self, tmp_path: Path) -> None:
        """非受限模式下可写入任意路径。"""
        outside = tmp_path.parent / "unrestricted_test"
        outside.mkdir(exist_ok=True)
        ctx = _make_ctx(str(tmp_path), restrict=False)
        tool = WriteFileTool.create(ctx)

        target = outside / "anywhere.txt"
        result = _run(tool.execute(path=str(target), content="anywhere"))

        assert not (isinstance(result, ToolResult) and result.is_error)
        assert target.exists()

        # 清理
        target.unlink()
        outside.rmdir()


# ---------------------------------------------------------------------------
# 错误处理
# ---------------------------------------------------------------------------


class TestWriteFileToolErrors:
    """WriteFileTool 参数校验与错误处理。"""

    def test_empty_path_returns_error(self, tmp_path: Path) -> None:
        """空路径参数返回错误。"""
        ctx = _make_ctx(str(tmp_path))
        tool = WriteFileTool.create(ctx)

        result = _run(tool.execute(path="", content="test"))

        assert isinstance(result, ToolResult)
        assert result.is_error
        assert "path" in str(result).lower()

    def test_none_content_returns_error(self, tmp_path: Path) -> None:
        """None content 参数返回错误。"""
        ctx = _make_ctx(str(tmp_path))
        tool = WriteFileTool.create(ctx)

        target = tmp_path / "none.txt"
        result = _run(tool.execute(path=str(target), content=None))  # type: ignore[arg-type]

        assert isinstance(result, ToolResult)
        assert result.is_error
        assert "content" in str(result).lower()


# ---------------------------------------------------------------------------
# 工具发现与注册
# ---------------------------------------------------------------------------


class TestWriteFileToolDiscovery:
    """WriteFileTool 被 ToolLoader 自动发现。"""

    def test_discovered_by_loader(self, tmp_path: Path) -> None:
        """ToolLoader 能发现并注册 WriteFileTool。"""
        ctx = _make_ctx(str(tmp_path))
        registry = ToolRegistry()
        loaded = ToolLoader().load(ctx, registry, scope="core")

        assert "write_file" in loaded
        assert registry.has("write_file")

    def test_tool_schema(self, tmp_path: Path) -> None:
        """WriteFileTool 的 to_schema 符合 OpenAI function 格式。"""
        ctx = _make_ctx(str(tmp_path))
        tool = WriteFileTool.create(ctx)
        schema = tool.to_schema()

        assert schema["type"] == "function"
        assert schema["function"]["name"] == "write_file"
        assert "path" in schema["function"]["parameters"]["properties"]
        assert "content" in schema["function"]["parameters"]["properties"]
        assert "path" in schema["function"]["parameters"]["required"]
        assert "content" in schema["function"]["parameters"]["required"]

    def test_not_read_only(self, tmp_path: Path) -> None:
        """WriteFileTool 非只读（有副作用）。"""
        ctx = _make_ctx(str(tmp_path))
        tool = WriteFileTool.create(ctx)
        assert tool.read_only is False


# ---------------------------------------------------------------------------
# _FsTool 基类
# ---------------------------------------------------------------------------


class TestFsToolBase:
    """_FsTool 基类行为。"""

    def test_enabled_reads_config(self, tmp_path: Path) -> None:
        """_FsTool.enabled 读取 config.tools.file.enable。"""
        ctx = _make_ctx(str(tmp_path))
        assert _FsTool.enabled(ctx) is True

        ctx.config.tools.file.enable = False
        assert _FsTool.enabled(ctx) is False

    def test_create_sets_workspace_and_restrict(self, tmp_path: Path) -> None:
        """_FsTool.create 正确设置 workspace 和 restrict。"""
        ctx = _make_ctx(str(tmp_path), restrict=True)
        tool = WriteFileTool.create(ctx)

        assert tool._workspace == str(tmp_path)
        assert tool._restrict is True

    def test_file_states_fallback(self, tmp_path: Path) -> None:
        """无显式 file_states 时使用 fallback 实例。"""
        tool = WriteFileTool(workspace=str(tmp_path))
        assert tool._file_states is tool._fallback_file_states

    def test_file_states_explicit(self, tmp_path: Path) -> None:
        """显式传入 file_states 时优先使用。"""
        states = FileStates()
        tool = WriteFileTool(workspace=str(tmp_path), file_states=states)
        assert tool._file_states is states
