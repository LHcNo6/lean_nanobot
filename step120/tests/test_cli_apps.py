"""step83：CliAppsTool 单元测试。"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from step120.tools.cli_apps import (
    CliApp,
    CliAppManager,
    CliAppsTool,
    build_cli_app_manager,
)


def _run(coro):
    return asyncio.run(coro)


class TestCliApp:
    """CliApp 数据类。"""

    def test_create(self) -> None:
        """创建 CliApp。"""
        app = CliApp(name="greet", command="echo", description="Greet app")
        assert app.name == "greet"
        assert app.command == "echo"
        assert app.description == "Greet app"

    def test_default_description(self) -> None:
        """默认描述为空字符串。"""
        app = CliApp(name="test", command="test")
        assert app.description == ""


class TestCliAppManager:
    """CliAppManager。"""

    def test_register_and_get(self) -> None:
        """注册和获取应用。"""
        manager = CliAppManager()
        app = CliApp(name="greet", command="echo")
        manager.register(app)
        assert manager.get("greet") is app

    def test_get_unknown_returns_none(self) -> None:
        """获取不存在的应用返回 None。"""
        manager = CliAppManager()
        assert manager.get("unknown") is None

    def test_list_names(self) -> None:
        """列出应用名称（排序）。"""
        manager = CliAppManager()
        manager.register(CliApp(name="zebra", command="z"))
        manager.register(CliApp(name="apple", command="a"))
        manager.register(CliApp(name="mango", command="m"))
        assert manager.list_names() == ["apple", "mango", "zebra"]

    def test_has(self) -> None:
        """检查应用是否存在。"""
        manager = CliAppManager()
        manager.register(CliApp(name="test", command="test"))
        assert manager.has("test")
        assert not manager.has("missing")

    def test_register_overwrite(self) -> None:
        """同名注册覆盖。"""
        manager = CliAppManager()
        manager.register(CliApp(name="test", command="old"))
        manager.register(CliApp(name="test", command="new"))
        assert manager.get("test").command == "new"

    def test_run_unknown_raises(self) -> None:
        """执行未知应用抛 ValueError。"""
        manager = CliAppManager()
        with pytest.raises(ValueError, match="Unknown CLI app"):
            _run(manager.run("unknown"))

    @patch("asyncio.create_subprocess_exec")
    def test_run_success(self, mock_exec) -> None:
        """执行成功。"""
        mock_process = MagicMock()
        mock_process.communicate = AsyncMock(return_value=(b"hello\n", b""))
        mock_process.returncode = 0
        mock_exec.return_value = mock_process

        manager = CliAppManager()
        manager.register(CliApp(name="greet", command=sys.executable))
        result = _run(manager.run("greet", args=["-c", "print('hello')"]))

        assert "hello" in result
        mock_exec.assert_called_once()

    @patch("asyncio.create_subprocess_exec")
    def test_run_with_stderr(self, mock_exec) -> None:
        """执行有 stderr 输出。"""
        mock_process = MagicMock()
        mock_process.communicate = AsyncMock(return_value=(b"", b"error msg\n"))
        mock_process.returncode = 1
        mock_exec.return_value = mock_process

        manager = CliAppManager()
        manager.register(CliApp(name="fail", command=sys.executable))
        result = _run(manager.run("fail"))

        assert "error msg" in result

    @patch("asyncio.create_subprocess_exec")
    def test_run_timeout(self, mock_exec) -> None:
        """执行超时。"""
        mock_process = MagicMock()
        mock_process.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
        mock_process.kill = MagicMock()
        mock_process.wait = AsyncMock()
        mock_exec.return_value = mock_process

        manager = CliAppManager()
        manager.register(CliApp(name="slow", command=sys.executable))

        with pytest.raises(asyncio.TimeoutError):
            _run(manager.run("slow", timeout=1))

        mock_process.kill.assert_called_once()

    @patch("asyncio.create_subprocess_exec")
    def test_run_passes_args(self, mock_exec) -> None:
        """参数正确传递。"""
        mock_process = MagicMock()
        mock_process.communicate = AsyncMock(return_value=(b"", b""))
        mock_process.returncode = 0
        mock_exec.return_value = mock_process

        manager = CliAppManager()
        manager.register(CliApp(name="test", command="myapp"))
        _run(manager.run("test", args=["--verbose", "input.txt"]))

        call_args = mock_exec.call_args
        assert call_args[0][0] == "myapp"
        assert call_args[0][1] == "--verbose"
        assert call_args[0][2] == "input.txt"


class TestCliAppsTool:
    """CliAppsTool。"""

    def _make_ctx(self, manager=None):
        from step120.tools.file_state import FileStateStore
        from step120.tools.cron import _CronStore
        from step120.context import ToolContext
        cfg = SimpleNamespace(
            exec=SimpleNamespace(enable=True, timeout=60, sandbox="", allowed_env_keys=[], allow_patterns=[], deny_patterns=[], path_prepend="", path_append=""),
            tools=SimpleNamespace(restrict_to_workspace=False, file=SimpleNamespace(enable=True)),
            web=SimpleNamespace(enable=True, timeout=30, user_agent="Test", search=SimpleNamespace(provider="duckduckgo", max_results=5, timeout=30)),
            my=SimpleNamespace(enable=True, allow_set=False),
            image_generation=SimpleNamespace(enabled=True, provider="simple", save_dir="generated"),
            cli_apps=SimpleNamespace(enable=True),
        )
        return ToolContext(
            config=cfg, workspace="C:/tmp", restrict_to_workspace=False,
            session_key="test", file_state_store=FileStateStore(),
            cron_store=_CronStore(), cli_app_manager=manager,
        )

    def test_create_with_manager(self) -> None:
        """create 时传入 manager。"""
        manager = CliAppManager()
        ctx = self._make_ctx(manager=manager)
        tool = CliAppsTool.create(ctx)
        assert tool._manager is manager

    def test_create_without_manager(self) -> None:
        """create 时不传 manager，创建空的。"""
        ctx = self._make_ctx(manager=None)
        tool = CliAppsTool.create(ctx)
        assert isinstance(tool._manager, CliAppManager)
        assert tool._manager.list_names() == []

    def test_name(self) -> None:
        """工具名。"""
        tool = CliAppsTool(manager=CliAppManager())
        assert tool.name == "run_cli_app"

    def test_description_with_apps(self) -> None:
        """描述包含已注册应用。"""
        manager = CliAppManager()
        manager.register(CliApp(name="greet", command="echo"))
        tool = CliAppsTool(manager=manager)
        assert "greet" in tool.description

    def test_description_no_apps(self) -> None:
        """描述无应用时提示。"""
        tool = CliAppsTool(manager=CliAppManager())
        assert "No CLI Apps" in tool.description

    def test_execute_missing_name(self) -> None:
        """缺少 name 参数报错。"""
        tool = CliAppsTool(manager=CliAppManager())
        result = _run(tool.execute(name=None))
        assert "required" in str(result).lower()

    def test_execute_unknown_app(self) -> None:
        """执行未知应用返回错误。"""
        tool = CliAppsTool(manager=CliAppManager())
        result = _run(tool.execute(name="unknown"))
        assert "Unknown CLI app" in str(result)

    @patch("asyncio.create_subprocess_exec")
    def test_execute_success(self, mock_exec) -> None:
        """执行成功。"""
        mock_process = MagicMock()
        mock_process.communicate = AsyncMock(return_value=(b"output\n", b""))
        mock_process.returncode = 0
        mock_exec.return_value = mock_process

        manager = CliAppManager()
        manager.register(CliApp(name="test", command=sys.executable))
        tool = CliAppsTool(manager=manager)

        result = _run(tool.execute(name="test", args=["-c", "print('output')"]))
        assert "output" in str(result)

    @patch("asyncio.create_subprocess_exec")
    def test_execute_timeout(self, mock_exec) -> None:
        """执行超时返回错误。"""
        mock_process = MagicMock()
        mock_process.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
        mock_process.kill = MagicMock()
        mock_process.wait = AsyncMock()
        mock_exec.return_value = mock_process

        manager = CliAppManager()
        manager.register(CliApp(name="slow", command=sys.executable))
        tool = CliAppsTool(manager=manager)

        result = _run(tool.execute(name="slow", timeout=1))
        assert "timed out" in str(result).lower()

    def test_enabled_default(self) -> None:
        """默认启用。"""
        ctx = self._make_ctx()
        assert CliAppsTool.enabled(ctx) is True

    def test_disabled(self) -> None:
        """可以禁用。"""
        ctx = self._make_ctx()
        ctx.config.cli_apps.enable = False
        assert CliAppsTool.enabled(ctx) is False


class TestBuildCliAppManager:
    """build_cli_app_manager 配置 -> 管理器（step120）。"""

    def test_none_returns_empty(self) -> None:
        """cfg=None 返回空管理器。"""
        mgr = build_cli_app_manager(None)
        assert isinstance(mgr, CliAppManager)
        assert mgr.list_names() == []

    def test_registers_apps_from_specs(self) -> None:
        """从 apps 列表注册应用（duck-typed）。"""
        cfg = SimpleNamespace(apps=[
            SimpleNamespace(name="lint", command="ruff", description="Lint"),
            SimpleNamespace(name="fmt", command="black"),
        ])
        mgr = build_cli_app_manager(cfg)
        names = mgr.list_names()
        assert names == ["fmt", "lint"]
        assert mgr.get("lint").command == "ruff"
        assert mgr.get("lint").description == "Lint"
        # 缺省 description 归一为空串
        assert mgr.get("fmt").description == ""

    def test_registers_from_schema_config(self) -> None:
        """从 schema CliAppsConfig 注册应用。"""
        from step120.config.schema import CliAppSpec, CliAppsConfig

        cfg = CliAppsConfig(apps=[
            CliAppSpec(name="hi", command="echo", description="say hi"),
        ])
        mgr = build_cli_app_manager(cfg)
        assert mgr.has("hi")
        assert mgr.get("hi").command == "echo"


class TestCliAppsToolRealExecution:
    """CliAppsTool 端到端真实执行（step120，已接线 manager）。"""

    def test_execute_runs_registered_app(self) -> None:
        """run_cli_app 实际执行已注册应用并返回输出。"""
        manager = CliAppManager()
        # argv 执行：command=python，args=['-c','print(...)']
        manager.register(CliApp(name="echoapp", command=sys.executable))
        tool = CliAppsTool(manager=manager)
        result = _run(tool.execute(name="echoapp", args=["-c", "print('hello-cli')"]))
        assert "hello-cli" in str(result)
