"""step99：MyTool 嵌套属性访问 + AgentLoop 引用单元测试。"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from step99.tools.self import (
    MyTool,
    _resolve_nested_path,
    _BLOCKED,
    _DENIED_ATTRS,
)


def _run(coro):
    return asyncio.run(coro)


def _make_ctx(agent_loop=None):
    from step99.tools.file_state import FileStateStore
    from step99.tools.cron import _CronStore
    from step99.context import ToolContext
    cfg = SimpleNamespace(
        exec=SimpleNamespace(enable=True, timeout=60, sandbox="", allowed_env_keys=[], allow_patterns=[], deny_patterns=[], path_prepend="", path_append=""),
        tools=SimpleNamespace(restrict_to_workspace=False, file=SimpleNamespace(enable=True)),
        web=SimpleNamespace(enable=True, timeout=30, user_agent="Test", search=SimpleNamespace(provider="duckduckgo", max_results=5, timeout=30, api_key="secret123")),
        my=SimpleNamespace(enable=True, allow_set=True),
        image_generation=SimpleNamespace(enabled=True, provider="simple", save_dir="generated"),
    )
    ctx = ToolContext(
        config=cfg, workspace="C:/tmp", restrict_to_workspace=False,
        session_key="test-session",
        file_state_store=FileStateStore(), cron_store=_CronStore(),
        agent_loop=agent_loop,
    )
    # iteration 不是 ToolContext 的字段，动态设置
    ctx.iteration = 5
    return ctx


class TestResolveNestedPath:
    """_resolve_nested_path 函数。"""

    def test_simple_attr(self) -> None:
        """简单属性访问。"""
        obj = SimpleNamespace(a=SimpleNamespace(b=42))
        result = _resolve_nested_path(obj, ["a", "b"])
        assert result == 42

    def test_none_base(self) -> None:
        """起始对象为 None。"""
        result = _resolve_nested_path(None, ["a", "b"])
        assert result is None

    def test_missing_attr(self) -> None:
        """不存在的属性返回 None。"""
        obj = SimpleNamespace(a=SimpleNamespace())
        result = _resolve_nested_path(obj, ["a", "missing"])
        assert result is None

    def test_denied_attr_raises(self) -> None:
        """Python 内部属性报错。"""
        obj = SimpleNamespace()
        with pytest.raises(PermissionError, match="Access denied"):
            _resolve_nested_path(obj, ["__class__"])

    def test_blocked_attr_raises(self) -> None:
        """BLOCKED 属性报错。"""
        obj = SimpleNamespace(tools=["t1"])
        with pytest.raises(PermissionError, match="blocked"):
            _resolve_nested_path(obj, ["tools"])

    def test_nested_denied_attr_raises(self) -> None:
        """嵌套路径中遇到禁止属性报错。"""
        obj = SimpleNamespace(a=SimpleNamespace(__dict__={}))
        with pytest.raises(PermissionError):
            _resolve_nested_path(obj, ["a", "__dict__"])


class TestMyToolNestedGet:
    """MyTool 嵌套属性 get。"""

    def test_config_exec_timeout(self) -> None:
        """嵌套访问 config.exec.timeout。"""
        ctx = _make_ctx()
        tool = MyTool(ctx=ctx, allow_set=False)
        result = _run(tool.execute(action="get", key="config.exec.timeout"))
        data = json.loads(str(result))
        assert data["value"] == 60

    def test_config_web_timeout(self) -> None:
        """嵌套访问 config.web.timeout。"""
        ctx = _make_ctx()
        tool = MyTool(ctx=ctx, allow_set=False)
        result = _run(tool.execute(action="get", key="config.web.timeout"))
        data = json.loads(str(result))
        assert data["value"] == 30

    def test_config_exec_enable(self) -> None:
        """嵌套访问 config.exec.enable。"""
        ctx = _make_ctx()
        tool = MyTool(ctx=ctx, allow_set=False)
        result = _run(tool.execute(action="get", key="config.exec.enable"))
        data = json.loads(str(result))
        assert data["value"] is True

    def test_nested_sensitive_masked(self) -> None:
        """嵌套访问中敏感字段被过滤。"""
        ctx = _make_ctx()
        tool = MyTool(ctx=ctx, allow_set=False)
        result = _run(tool.execute(action="get", key="config.web.search"))
        data = json.loads(str(result))
        # api_key 应该被过滤为 ***
        assert data["value"]["api_key"] == "***"

    def test_nested_missing_returns_none(self) -> None:
        """不存在的嵌套属性返回 None（不报错）。"""
        ctx = _make_ctx()
        tool = MyTool(ctx=ctx, allow_set=False)
        result = _run(tool.execute(action="get", key="config.nonexistent.field"))
        data = json.loads(str(result))
        assert data["value"] is None

    def test_nested_blocked_attr_error(self) -> None:
        """嵌套路径中遇到 BLOCKED 属性返回错误。"""
        ctx = _make_ctx()
        tool = MyTool(ctx=ctx, allow_set=False)
        # config.tools 是允许的，但 tools 本身在 BLOCKED 中
        # 注意：config.tools 中的 tools 是 config 的属性，不是顶级 BLOCKED
        # 测试直接访问 blocked 的顶级路径
        agent_loop = SimpleNamespace(tools=["t1", "t2"])
        ctx2 = _make_ctx(agent_loop=agent_loop)
        tool2 = MyTool(ctx=ctx2, allow_set=False)
        result = _run(tool2.execute(action="get", key="agent.tools"))
        assert "blocked" in str(result).lower() or "error" in str(result).lower()

    def test_nested_denied_attr_error(self) -> None:
        """嵌套路径中遇到 Python 内部属性返回错误。"""
        ctx = _make_ctx()
        tool = MyTool(ctx=ctx, allow_set=False)
        result = _run(tool.execute(action="get", key="config.__class__"))
        assert "denied" in str(result).lower() or "error" in str(result).lower()


class TestMyToolAgentKey:
    """MyTool agent key（AgentLoop 引用）。"""

    def test_agent_key_maps_to_agent_loop(self) -> None:
        """agent key 映射到 ctx.agent_loop。"""
        agent_loop = SimpleNamespace(iteration=10, status="running")
        ctx = _make_ctx(agent_loop=agent_loop)
        tool = MyTool(ctx=ctx, allow_set=False)
        result = _run(tool.execute(action="get", key="agent"))
        data = json.loads(str(result))
        assert data["value"]["iteration"] == 10
        assert data["value"]["status"] == "running"

    def test_agent_nested_attr(self) -> None:
        """agent 嵌套属性访问。"""
        agent_loop = SimpleNamespace(config=SimpleNamespace(max_steps=100))
        ctx = _make_ctx(agent_loop=agent_loop)
        tool = MyTool(ctx=ctx, allow_set=False)
        result = _run(tool.execute(action="get", key="agent.config.max_steps"))
        data = json.loads(str(result))
        assert data["value"] == 100

    def test_agent_none_when_no_loop(self) -> None:
        """没有 agent_loop 时 agent key 返回 None。"""
        ctx = _make_ctx(agent_loop=None)
        tool = MyTool(ctx=ctx, allow_set=False)
        result = _run(tool.execute(action="get", key="agent"))
        data = json.loads(str(result))
        assert data["value"] is None


class TestMyToolBackwardCompat:
    """单层属性向后兼容。"""

    def test_workspace(self) -> None:
        """workspace 单层属性。"""
        ctx = _make_ctx()
        tool = MyTool(ctx=ctx, allow_set=False)
        result = _run(tool.execute(action="get", key="workspace"))
        data = json.loads(str(result))
        assert data["value"] == "C:/tmp"

    def test_session_key(self) -> None:
        """session_key 单层属性。"""
        ctx = _make_ctx()
        tool = MyTool(ctx=ctx, allow_set=False)
        result = _run(tool.execute(action="get", key="session_key"))
        data = json.loads(str(result))
        assert data["value"] == "test-session"

    def test_iteration(self) -> None:
        """iteration 单层属性。"""
        ctx = _make_ctx()
        tool = MyTool(ctx=ctx, allow_set=False)
        result = _run(tool.execute(action="get", key="iteration"))
        data = json.loads(str(result))
        assert data["value"] == 5

    def test_exec_timeout(self) -> None:
        """exec_timeout 单层属性。"""
        ctx = _make_ctx()
        tool = MyTool(ctx=ctx, allow_set=False)
        result = _run(tool.execute(action="get", key="exec_timeout"))
        data = json.loads(str(result))
        assert data["value"] == 60

    def test_unknown_key_error(self) -> None:
        """未知 key 返回错误。"""
        ctx = _make_ctx()
        tool = MyTool(ctx=ctx, allow_set=False)
        result = _run(tool.execute(action="get", key="nonexistent"))
        assert "unknown" in str(result).lower() or "error" in str(result).lower()


class TestMyToolSetStillSingleLevel:
    """set 操作仍然只支持单层。"""

    def test_set_exec_timeout(self) -> None:
        """set exec_timeout 单层属性。"""
        ctx = _make_ctx()
        tool = MyTool(ctx=ctx, allow_set=True)
        result = _run(tool.execute(action="set", key="exec_timeout", value="120"))
        data = json.loads(str(result))
        assert data["status"] == "set"
        assert ctx.config.exec.timeout == 120

    def test_nested_set_not_supported(self) -> None:
        """嵌套 set 不支持（key 不在白名单中）。"""
        ctx = _make_ctx()
        tool = MyTool(ctx=ctx, allow_set=True)
        result = _run(tool.execute(action="set", key="config.exec.timeout", value="120"))
        assert "cannot be set" in str(result) or "error" in str(result).lower()
