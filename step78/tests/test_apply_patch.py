"""step74：ApplyPatchTool 单元测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from step78.context import ToolContext
from step78.loader import ToolLoader
from step78.tool import ToolRegistry, ToolResult
from step78.tools.apply_patch import ApplyPatchTool


def _make_config() -> SimpleNamespace:
    return SimpleNamespace(
        exec=SimpleNamespace(enable=True, timeout=60, sandbox="", allowed_env_keys=[], allow_patterns=[], deny_patterns=[], path_prepend="", path_append=""),
        tools=SimpleNamespace(
            restrict_to_workspace=False,
            file=SimpleNamespace(enable=True),
        ),
        web=SimpleNamespace(enable=True, timeout=30, user_agent="Test", search=SimpleNamespace(provider="duckduckgo", max_results=5, timeout=30)),
    )


def _make_ctx(workspace: str) -> ToolContext:
    from step78.tools.file_state import FileStateStore
    return ToolContext(
        config=_make_config(),
        workspace=workspace,
        restrict_to_workspace=False,
        session_key="test-session",
        file_state_store=FileStateStore(),
    )


def _run(coro):
    return asyncio.run(coro)


class TestApplyPatchReplace:
    """replace 操作。"""

    def test_replace_single_match(self, tmp_path: Path) -> None:
        """精确替换单处文本。"""
        f = tmp_path / "test.py"
        f.write_text("hello world\nfoo bar\n")

        ctx = _make_ctx(str(tmp_path))
        tool = ApplyPatchTool.create(ctx)

        result = _run(tool.execute(edits=[
            {"path": "test.py", "action": "replace", "old_text": "hello world", "new_text": "hi there"}
        ]))

        assert "Patch applied" in str(result)
        assert "hi there" in f.read_text()
        assert "hello world" not in f.read_text()

    def test_replace_multiple_matches_error(self, tmp_path: Path) -> None:
        """old_text 多处匹配时报错。"""
        f = tmp_path / "test.py"
        f.write_text("foo\nfoo\n")

        ctx = _make_ctx(str(tmp_path))
        tool = ApplyPatchTool.create(ctx)

        result = _run(tool.execute(edits=[
            {"path": "test.py", "action": "replace", "old_text": "foo", "new_text": "bar"}
        ]))
        assert isinstance(result, ToolResult) and result.is_error
        assert "multiple times" in str(result)

    def test_replace_not_found_error(self, tmp_path: Path) -> None:
        """old_text 不存在时报错。"""
        f = tmp_path / "test.py"
        f.write_text("hello\n")

        ctx = _make_ctx(str(tmp_path))
        tool = ApplyPatchTool.create(ctx)

        result = _run(tool.execute(edits=[
            {"path": "test.py", "action": "replace", "old_text": "nonexistent", "new_text": "x"}
        ]))
        assert isinstance(result, ToolResult) and result.is_error
        assert "not found" in str(result)

    def test_replace_file_not_exist_error(self, tmp_path: Path) -> None:
        """replace 不存在的文件时报错。"""
        ctx = _make_ctx(str(tmp_path))
        tool = ApplyPatchTool.create(ctx)

        result = _run(tool.execute(edits=[
            {"path": "nonexistent.py", "action": "replace", "old_text": "a", "new_text": "b"}
        ]))
        assert isinstance(result, ToolResult) and result.is_error

    def test_replace_preserves_crlf(self, tmp_path: Path) -> None:
        """CRLF 文件保留换行符。"""
        f = tmp_path / "test.py"
        f.write_bytes(b"hello\r\nworld\r\n")

        ctx = _make_ctx(str(tmp_path))
        tool = ApplyPatchTool.create(ctx)

        _run(tool.execute(edits=[
            {"path": "test.py", "action": "replace", "old_text": "hello", "new_text": "hi"}
        ]))

        raw = f.read_bytes()
        assert b"\r\n" in raw


class TestApplyPatchAdd:
    """add 操作。"""

    def test_add_to_existing_file(self, tmp_path: Path) -> None:
        """追加到现有文件。"""
        f = tmp_path / "test.py"
        f.write_text("line1\n")

        ctx = _make_ctx(str(tmp_path))
        tool = ApplyPatchTool.create(ctx)

        result = _run(tool.execute(edits=[
            {"path": "test.py", "action": "add", "new_text": "line2\n"}
        ]))

        assert "update" in str(result)
        content = f.read_text()
        assert "line1" in content
        assert "line2" in content

    def test_add_creates_new_file(self, tmp_path: Path) -> None:
        """add 创建新文件。"""
        ctx = _make_ctx(str(tmp_path))
        tool = ApplyPatchTool.create(ctx)

        result = _run(tool.execute(edits=[
            {"path": "new.py", "action": "add", "new_text": "print('new')\n"}
        ]))

        assert "add" in str(result)
        assert (tmp_path / "new.py").exists()
        assert "print('new')" in (tmp_path / "new.py").read_text()

    def test_add_missing_new_text_error(self, tmp_path: Path) -> None:
        """add 缺少 new_text 报错。"""
        ctx = _make_ctx(str(tmp_path))
        tool = ApplyPatchTool.create(ctx)

        result = _run(tool.execute(edits=[
            {"path": "test.py", "action": "add"}
        ]))
        assert isinstance(result, ToolResult) and result.is_error


class TestApplyPatchMultiFile:
    """多文件批量编辑。"""

    def test_multiple_files(self, tmp_path: Path) -> None:
        """单次调用编辑多个文件。"""
        (tmp_path / "a.py").write_text("aaa\n")
        (tmp_path / "b.py").write_text("bbb\n")

        ctx = _make_ctx(str(tmp_path))
        tool = ApplyPatchTool.create(ctx)

        result = _run(tool.execute(edits=[
            {"path": "a.py", "action": "replace", "old_text": "aaa", "new_text": "AAA"},
            {"path": "b.py", "action": "replace", "old_text": "bbb", "new_text": "BBB"},
        ]))

        assert "Patch applied" in str(result)
        assert "AAA" in (tmp_path / "a.py").read_text()
        assert "BBB" in (tmp_path / "b.py").read_text()

    def test_chained_edits_same_file(self, tmp_path: Path) -> None:
        """同文件链式编辑。"""
        f = tmp_path / "test.py"
        f.write_text("foo\n")

        ctx = _make_ctx(str(tmp_path))
        tool = ApplyPatchTool.create(ctx)

        result = _run(tool.execute(edits=[
            {"path": "test.py", "action": "replace", "old_text": "foo", "new_text": "bar"},
            {"path": "test.py", "action": "add", "new_text": "baz\n"},
        ]))

        assert "Patch applied" in str(result)
        content = f.read_text()
        assert "bar" in content
        assert "baz" in content


class TestApplyPatchDryRun:
    """dry_run 模式。"""

    def test_dry_run_does_not_write(self, tmp_path: Path) -> None:
        """dry_run 不写入文件。"""
        f = tmp_path / "test.py"
        original = "hello\n"
        f.write_text(original)

        ctx = _make_ctx(str(tmp_path))
        tool = ApplyPatchTool.create(ctx)

        result = _run(tool.execute(
            edits=[{"path": "test.py", "action": "replace", "old_text": "hello", "new_text": "hi"}],
            dry_run=True,
        ))

        assert "dry-run" in str(result)
        assert f.read_text() == original  # 文件未修改


class TestApplyPatchValidation:
    """参数校验。"""

    def test_empty_edits_error(self, tmp_path: Path) -> None:
        """空 edits 报错。"""
        ctx = _make_ctx(str(tmp_path))
        tool = ApplyPatchTool.create(ctx)
        result = _run(tool.execute(edits=[]))
        assert isinstance(result, ToolResult) and result.is_error

    def test_unknown_action_error(self, tmp_path: Path) -> None:
        """未知 action 报错。"""
        ctx = _make_ctx(str(tmp_path))
        tool = ApplyPatchTool.create(ctx)
        result = _run(tool.execute(edits=[
            {"path": "test.py", "action": "delete"}
        ]))
        assert isinstance(result, ToolResult) and result.is_error

    def test_missing_path_error(self, tmp_path: Path) -> None:
        """缺少 path 报错。"""
        ctx = _make_ctx(str(tmp_path))
        tool = ApplyPatchTool.create(ctx)
        result = _run(tool.execute(edits=[
            {"action": "add", "new_text": "x"}
        ]))
        assert isinstance(result, ToolResult) and result.is_error


class TestApplyPatchDiscovery:
    """工具发现。"""

    def test_discovered(self, tmp_path: Path) -> None:
        """ToolLoader 自动发现 apply_patch。"""
        ctx = _make_ctx(str(tmp_path))
        registry = ToolRegistry()
        loaded = ToolLoader().load(ctx, registry, scope="core")
        assert "apply_patch" in loaded
        assert registry.has("apply_patch")

    def test_schema(self, tmp_path: Path) -> None:
        """参数 schema 正确。"""
        ctx = _make_ctx(str(tmp_path))
        tool = ApplyPatchTool.create(ctx)
        schema = tool.to_schema()
        assert schema["function"]["name"] == "apply_patch"
        props = schema["function"]["parameters"]["properties"]
        assert "edits" in props
        assert "dry_run" in props
