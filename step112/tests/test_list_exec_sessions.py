"""step84：ListExecSessionsTool 单元测试。"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from step112.tools.exec_session import (
    ExecSessionInfo,
    ExecSessionManager,
    ListExecSessionsTool,
)


def _run(coro):
    return asyncio.run(coro)


def _make_mock_session(session_id="s1", command="echo hello", cwd="/tmp", returncode=None):
    """创建 mock _ExecSession。"""
    session = MagicMock()
    session.session_id = session_id
    session.command = command
    session.cwd = cwd
    session.started_at = time.monotonic() - 10  # 10秒前启动
    session.process.returncode = returncode
    return session


class TestExecSessionInfo:
    """ExecSessionInfo 数据类。"""

    def test_create(self) -> None:
        """创建 ExecSessionInfo。"""
        info = ExecSessionInfo(
            session_id="s1",
            command="echo hello",
            cwd="/tmp",
            elapsed_s=10.5,
            status="running",
            returncode=None,
        )
        assert info.session_id == "s1"
        assert info.status == "running"
        assert info.returncode is None

    def test_exited_status(self) -> None:
        """已退出会话。"""
        info = ExecSessionInfo(
            session_id="s2", command="ls", cwd="/tmp",
            elapsed_s=5.0, status="exited", returncode=0,
        )
        assert info.status == "exited"
        assert info.returncode == 0


class TestExecSessionManagerList:
    """ExecSessionManager.list()。"""

    def test_empty_list(self) -> None:
        """空管理器返回空列表。"""
        manager = ExecSessionManager()
        assert manager.list() == []

    def test_list_running_session(self) -> None:
        """列出运行中的会话。"""
        manager = ExecSessionManager()
        session = _make_mock_session(returncode=None)
        manager._sessions["s1"] = session

        infos = manager.list()
        assert len(infos) == 1
        assert infos[0].session_id == "s1"
        assert infos[0].status == "running"
        assert infos[0].returncode is None
        assert infos[0].elapsed_s >= 0

    def test_list_exited_session(self) -> None:
        """列出已退出的会话。"""
        manager = ExecSessionManager()
        session = _make_mock_session(returncode=0)
        manager._sessions["s1"] = session

        infos = manager.list()
        assert len(infos) == 1
        assert infos[0].status == "exited"
        assert infos[0].returncode == 0

    def test_list_multiple_sessions(self) -> None:
        """列出多个会话。"""
        manager = ExecSessionManager()
        manager._sessions["s1"] = _make_mock_session("s1", returncode=None)
        manager._sessions["s2"] = _make_mock_session("s2", returncode=1)

        infos = manager.list()
        assert len(infos) == 2
        ids = {info.session_id for info in infos}
        assert ids == {"s1", "s2"}

    def test_list_preserves_command_and_cwd(self) -> None:
        """列表保留命令和工作目录。"""
        manager = ExecSessionManager()
        session = _make_mock_session(command="python script.py", cwd="/home/user")
        manager._sessions["s1"] = session

        infos = manager.list()
        assert infos[0].command == "python script.py"
        assert infos[0].cwd == "/home/user"


class TestListExecSessionsTool:
    """ListExecSessionsTool。"""

    def _make_ctx(self, manager=None):
        from step112.tools.file_state import FileStateStore
        from step112.tools.cron import _CronStore
        from step112.context import ToolContext
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
            cron_store=_CronStore(), exec_session_manager=manager,
        )

    def test_name(self) -> None:
        """工具名。"""
        tool = ListExecSessionsTool()
        assert tool.name == "list_exec_sessions"

    def test_read_only(self) -> None:
        """只读工具。"""
        tool = ListExecSessionsTool()
        assert tool.read_only is True

    def test_description(self) -> None:
        """工具描述。"""
        tool = ListExecSessionsTool()
        assert "exec sessions" in tool.description.lower()

    def test_enabled_with_manager(self) -> None:
        """有 manager 时启用。"""
        ctx = self._make_ctx(manager=ExecSessionManager())
        assert ListExecSessionsTool.enabled(ctx) is True

    def test_disabled_without_manager(self) -> None:
        """无 manager 时禁用。"""
        ctx = self._make_ctx(manager=None)
        assert ListExecSessionsTool.enabled(ctx) is False

    def test_create_with_manager(self) -> None:
        """create 注入 manager。"""
        manager = ExecSessionManager()
        ctx = self._make_ctx(manager=manager)
        tool = ListExecSessionsTool.create(ctx)
        assert tool._session_manager is manager

    def test_execute_no_sessions(self) -> None:
        """无会话时返回提示。"""
        manager = ExecSessionManager()
        tool = ListExecSessionsTool()
        tool._session_manager = manager

        result = _run(tool.execute())
        assert "No active exec sessions" in str(result)

    def test_execute_with_sessions(self) -> None:
        """有会话时返回列表。"""
        manager = ExecSessionManager()
        manager._sessions["s1"] = _make_mock_session("s1", command="echo hello", cwd="/tmp")
        tool = ListExecSessionsTool()
        tool._session_manager = manager

        result = _run(tool.execute())
        result_str = str(result)
        assert "s1" in result_str
        assert "running" in result_str
        assert "echo hello" in result_str
        assert "/tmp" in result_str

    def test_execute_exited_session(self) -> None:
        """已退出会话显示 exited。"""
        manager = ExecSessionManager()
        manager._sessions["s1"] = _make_mock_session("s1", returncode=0)
        tool = ListExecSessionsTool()
        tool._session_manager = manager

        result = _run(tool.execute())
        assert "exited" in str(result)

    def test_execute_long_command_truncated(self) -> None:
        """长命令被截断。"""
        manager = ExecSessionManager()
        long_cmd = "x" * 200
        manager._sessions["s1"] = _make_mock_session("s1", command=long_cmd)
        tool = ListExecSessionsTool()
        tool._session_manager = manager

        result = _run(tool.execute())
        result_str = str(result)
        assert "..." in result_str
        # 截断后命令部分不超过120字符
        assert len(long_cmd) > 120

    def test_execute_no_manager_error(self) -> None:
        """无 manager 时返回错误。"""
        tool = ListExecSessionsTool()
        tool._session_manager = None

        result = _run(tool.execute())
        assert "No exec session manager" in str(result)

    def test_execute_multiple_sessions(self) -> None:
        """多个会话每行一个。"""
        manager = ExecSessionManager()
        manager._sessions["s1"] = _make_mock_session("s1", command="cmd1")
        manager._sessions["s2"] = _make_mock_session("s2", command="cmd2")
        tool = ListExecSessionsTool()
        tool._session_manager = manager

        result = _run(tool.execute())
        result_str = str(result)
        assert "s1" in result_str
        assert "s2" in result_str
        assert "\n" in result_str
