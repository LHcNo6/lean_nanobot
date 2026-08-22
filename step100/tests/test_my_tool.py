"""step75：MyTool 运行时自省单元测试。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from step100.context import ToolContext
from step100.loader import ToolLoader
from step100.tool import ToolRegistry, ToolResult
from step100.tools.self import MyTool


def _make_config(*, my_enable: bool = True, my_allow_set: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        exec=SimpleNamespace(enable=True, timeout=60, sandbox="", allowed_env_keys=[], allow_patterns=[], deny_patterns=[], path_prepend="", path_append=""),
        tools=SimpleNamespace(restrict_to_workspace=False, file=SimpleNamespace(enable=True)),
        web=SimpleNamespace(enable=True, timeout=30, user_agent="Test", search=SimpleNamespace(provider="duckduckgo", max_results=5, timeout=30)),
        my=SimpleNamespace(enable=my_enable, allow_set=my_allow_set),
    )


def _make_ctx(workspace: str, **kwargs) -> ToolContext:
    from step100.tools.file_state import FileStateStore
    return ToolContext(
        config=_make_config(**kwargs),
        workspace=workspace,
        restrict_to_workspace=False,
        session_key="test-session",
        file_state_store=FileStateStore(),
    )


def _run(coro):
    return asyncio.run(coro)


class TestMyToolGet:
    """get 操作。"""

    def test_get_workspace(self, tmp_path: Path) -> None:
        """获取 workspace。"""
        ctx = _make_ctx(str(tmp_path))
        tool = MyTool.create(ctx)

        result = _run(tool.execute(action="get", key="workspace"))
        data = json.loads(str(result))

        assert data["key"] == "workspace"
        assert tmp_path.name in data["value"]

    def test_get_exec_timeout(self, tmp_path: Path) -> None:
        """获取 exec_timeout。"""
        ctx = _make_ctx(str(tmp_path))
        tool = MyTool.create(ctx)

        result = _run(tool.execute(action="get", key="exec_timeout"))
        data = json.loads(str(result))

        assert data["value"] == 60

    def test_get_config(self, tmp_path: Path) -> None:
        """获取 config（不报错）。"""
        ctx = _make_ctx(str(tmp_path))
        tool = MyTool.create(ctx)

        result = _run(tool.execute(action="get", key="config"))
        assert isinstance(result, str)
        assert "value" in str(result)

    def test_get_unknown_key_error(self, tmp_path: Path) -> None:
        """未知 key 报错。"""
        ctx = _make_ctx(str(tmp_path))
        tool = MyTool.create(ctx)

        result = _run(tool.execute(action="get", key="nonexistent_key"))
        assert isinstance(result, ToolResult)
        assert result.is_error


class TestMyToolSet:
    """set 操作。"""

    def test_set_disabled_by_default(self, tmp_path: Path) -> None:
        """默认禁止 set。"""
        ctx = _make_ctx(str(tmp_path))
        tool = MyTool.create(ctx)

        result = _run(tool.execute(action="set", key="exec_timeout", value="120"))
        assert isinstance(result, ToolResult)
        assert result.is_error
        assert "disabled" in str(result).lower()

    def test_set_enabled(self, tmp_path: Path) -> None:
        """allow_set=True 时可以修改。"""
        ctx = _make_ctx(str(tmp_path), my_allow_set=True)
        tool = MyTool.create(ctx)

        result = _run(tool.execute(action="set", key="exec_timeout", value="120"))
        data = json.loads(str(result))

        assert data["status"] == "set"
        assert data["value"] == "120"
        assert ctx.config.exec.timeout == 120

    def test_set_readonly_rejected(self, tmp_path: Path) -> None:
        """READ_ONLY 属性禁止修改。"""
        ctx = _make_ctx(str(tmp_path), my_allow_set=True)
        tool = MyTool.create(ctx)

        result = _run(tool.execute(action="set", key="workspace", value="/tmp"))
        assert isinstance(result, ToolResult)
        assert result.is_error
        assert "read-only" in str(result).lower()

    def test_set_not_settable_rejected(self, tmp_path: Path) -> None:
        """不可设置的属性被拒绝。"""
        ctx = _make_ctx(str(tmp_path), my_allow_set=True)
        tool = MyTool.create(ctx)

        result = _run(tool.execute(action="set", key="random_key", value="x"))
        assert isinstance(result, ToolResult)
        assert result.is_error

    def test_set_missing_value(self, tmp_path: Path) -> None:
        """set 缺少 value 报错。"""
        ctx = _make_ctx(str(tmp_path), my_allow_set=True)
        tool = MyTool.create(ctx)

        result = _run(tool.execute(action="set", key="exec_timeout"))
        assert isinstance(result, ToolResult)
        assert result.is_error


class TestMyToolSecurity:
    """安全边界。"""

    def test_blocked_access_denied(self, tmp_path: Path) -> None:
        """BLOCKED 属性禁止访问。"""
        ctx = _make_ctx(str(tmp_path))
        tool = MyTool.create(ctx)

        result = _run(tool.execute(action="get", key="tools"))
        assert isinstance(result, ToolResult)
        assert result.is_error
        assert "blocked" in str(result).lower()

    def test_denied_attrs(self, tmp_path: Path) -> None:
        """Python 内部属性禁止访问。"""
        ctx = _make_ctx(str(tmp_path))
        tool = MyTool.create(ctx)

        result = _run(tool.execute(action="get", key="__class__"))
        assert isinstance(result, ToolResult)
        assert result.is_error

    def test_sensitive_value_masked(self, tmp_path: Path) -> None:
        """敏感字段值被过滤。"""
        from step100.tools.self import _safe_repr

        data = {"api_key": "secret123", "normal": "value"}
        result = _safe_repr(data)

        assert result["api_key"] == "***"
        assert result["normal"] == "value"

    def test_unknown_action_error(self, tmp_path: Path) -> None:
        """未知 action 报错。"""
        ctx = _make_ctx(str(tmp_path))
        tool = MyTool.create(ctx)

        result = _run(tool.execute(action="delete", key="x"))
        assert isinstance(result, ToolResult)
        assert result.is_error

    def test_missing_action_error(self, tmp_path: Path) -> None:
        """缺少 action 报错。"""
        ctx = _make_ctx(str(tmp_path))
        tool = MyTool.create(ctx)

        result = _run(tool.execute(key="workspace"))
        assert isinstance(result, ToolResult)
        assert result.is_error

    def test_missing_key_error(self, tmp_path: Path) -> None:
        """缺少 key 报错。"""
        ctx = _make_ctx(str(tmp_path))
        tool = MyTool.create(ctx)

        result = _run(tool.execute(action="get"))
        assert isinstance(result, ToolResult)
        assert result.is_error


class TestMyToolDiscovery:
    """工具发现。"""

    def test_discovered(self, tmp_path: Path) -> None:
        """ToolLoader 自动发现 my。"""
        ctx = _make_ctx(str(tmp_path))
        registry = ToolRegistry()
        loaded = ToolLoader().load(ctx, registry, scope="core")

        assert "my" in loaded
        assert registry.has("my")

    def test_disabled_not_loaded(self, tmp_path: Path) -> None:
        """config.my.enable=False 时不加载。"""
        ctx = _make_ctx(str(tmp_path), my_enable=False)
        registry = ToolRegistry()
        loaded = ToolLoader().load(ctx, registry, scope="core")

        assert "my" not in loaded

    def test_schema(self, tmp_path: Path) -> None:
        """参数 schema 正确。"""
        ctx = _make_ctx(str(tmp_path))
        tool = MyTool.create(ctx)
        schema = tool.to_schema()

        assert schema["function"]["name"] == "my"
        props = schema["function"]["parameters"]["properties"]
        assert "action" in props
        assert "key" in props
        assert "value" in props
