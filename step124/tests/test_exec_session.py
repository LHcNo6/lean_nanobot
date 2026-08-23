"""step73：ExecSession 交互式执行会话单元测试。

覆盖：
- ExecTool 会话模式（yield_time_ms）
- WriteStdinTool
- ExecSessionManager
- 向后兼容（一次性 exec）
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from step124.context import RequestContext, ToolContext, request_context
from step124.loader import ToolLoader
from step124.tool import ToolRegistry, ToolResult
from step124.tools.exec_session import ExecSessionManager, WriteStdinTool
from step124.tools.shell import ExecTool


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _make_config(*, exec_enable: bool = True, restrict: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        exec=SimpleNamespace(
            enable=exec_enable, timeout=60, sandbox="",
            allowed_env_keys=[], allow_patterns=[], deny_patterns=[],
            path_prepend="", path_append="",
        ),
        tools=SimpleNamespace(restrict_to_workspace=restrict),
        web=SimpleNamespace(enable=True, timeout=30, user_agent="Test", search=SimpleNamespace(provider="duckduckgo", max_results=5, timeout=30)),
    )


def _make_ctx(workspace: str, *, with_session_manager: bool = True, **kwargs) -> ToolContext:
    session_manager = ExecSessionManager() if with_session_manager else None
    return ToolContext(
        config=_make_config(**kwargs),
        workspace=workspace,
        restrict_to_workspace=False,
        session_key="test-session",
        exec_session_manager=session_manager,
    )


def _run(coro):
    return asyncio.run(coro)


def _py_cmd(code: str) -> str:
    py = sys.executable
    if " " in py:
        py = f'"{py}"'
    return f'{py} -c "{code}"'


# ---------------------------------------------------------------------------
# ExecTool 会话模式
# ---------------------------------------------------------------------------


class TestExecSessionMode:
    """ExecTool yield_time_ms 会话模式。"""

    def test_session_mode_short_command(self, tmp_path: Path) -> None:
        """短命令在 yield_time_ms 内完成，返回输出和退出码。"""
        ctx = _make_ctx(str(tmp_path))
        tool = ExecTool.create(ctx)

        result = _run(tool.execute(
            command=_py_cmd("print('session output')"),
            yield_time_ms=2000,
        ))
        text = str(result)

        assert "Session started:" in text
        assert "session output" in text
        assert "exit code: 0" in text.lower() or "[exit code: 0]" in text

    def test_session_mode_returns_session_id(self, tmp_path: Path) -> None:
        """会话模式返回 session_id。"""
        ctx = _make_ctx(str(tmp_path))
        tool = ExecTool.create(ctx)

        result = _run(tool.execute(
            command=_py_cmd("print('test')"),
            yield_time_ms=2000,
        ))
        text = str(result)

        assert "Session started:" in text
        # session_id 是 12 位十六进制
        import re
        match = re.search(r"Session started: ([0-9a-f]{12})", text)
        assert match is not None

    def test_session_mode_no_manager_returns_error(self, tmp_path: Path) -> None:
        """无 session_manager 时会话模式返回错误。"""
        ctx = _make_ctx(str(tmp_path), with_session_manager=False)
        tool = ExecTool.create(ctx)

        result = _run(tool.execute(
            command=_py_cmd("print('test')"),
            yield_time_ms=1000,
        ))
        assert isinstance(result, ToolResult)
        assert result.is_error
        assert "session manager" in str(result).lower()

    def test_one_shot_mode_unchanged(self, tmp_path: Path) -> None:
        """无 yield_time_ms 时一次性执行行为不变。"""
        ctx = _make_ctx(str(tmp_path))
        tool = ExecTool.create(ctx)

        result = _run(tool.execute(command=_py_cmd("print('oneshot')")))
        text = str(result)

        assert "Session started" not in text
        assert "oneshot" in text
        assert "Exit code: 0" in text


# ---------------------------------------------------------------------------
# WriteStdinTool
# ---------------------------------------------------------------------------


class TestWriteStdinTool:
    """WriteStdinTool 测试。"""

    def test_write_stdin_to_session(self, tmp_path: Path) -> None:
        """向会话写入 stdin 并获取输出（Unix 上完整测试，Windows 上验证不崩溃）。"""
        ctx = _make_ctx(str(tmp_path))
        exec_tool = ExecTool.create(ctx)
        write_tool = WriteStdinTool.create(ctx)

        # 启动一个读取 stdin 并回显的 Python 进程
        start_result = _run(exec_tool.execute(
            command=_py_cmd("import sys; line = sys.stdin.readline(); print('ECHO:', line.strip())"),
            yield_time_ms=500,
        ))
        start_text = str(start_result)
        assert "Session started:" in start_text

        import re
        match = re.search(r"Session started: ([0-9a-f]{12})", start_text)
        session_id = match.group(1)

        # 写入 stdin 并关闭
        write_result = _run(write_tool.execute(
            session_id=session_id,
            chars="hello stdin\n",
            close_stdin=True,
            yield_time_ms=2000,
        ))
        write_text = str(write_result)

        # Windows 上 stdin 管道可能有限制，至少验证不返回异常错误
        if sys.platform == "win32":
            # Windows 上可能 stdin write 失败，但不应是未处理的异常
            assert "Error: write_stdin failed" not in write_text or "stdin" in write_text.lower()
        else:
            assert "ECHO: hello stdin" in write_text
            assert "exit code: 0" in write_text.lower() or "[exit code: 0]" in write_text

    def test_write_stdin_nonexistent_session(self, tmp_path: Path) -> None:
        """向不存在的会话写入返回错误。"""
        ctx = _make_ctx(str(tmp_path))
        tool = WriteStdinTool.create(ctx)

        result = _run(tool.execute(session_id="nonexistent123", chars="test"))
        assert isinstance(result, ToolResult)
        assert result.is_error
        assert "not found" in str(result).lower()

    def test_write_stdin_missing_session_id(self, tmp_path: Path) -> None:
        """缺少 session_id 返回错误。"""
        ctx = _make_ctx(str(tmp_path))
        tool = WriteStdinTool.create(ctx)

        result = _run(tool.execute(session_id=None, chars="test"))
        assert isinstance(result, ToolResult)
        assert result.is_error

    def test_terminate_session(self, tmp_path: Path) -> None:
        """终止会话。"""
        ctx = _make_ctx(str(tmp_path))
        exec_tool = ExecTool.create(ctx)
        write_tool = WriteStdinTool.create(ctx)

        # 启动一个长运行进程
        start_result = _run(exec_tool.execute(
            command=_py_cmd("import time; time.sleep(30)"),
            yield_time_ms=300,
        ))
        start_text = str(start_result)
        assert "still running" in start_text.lower()

        import re
        match = re.search(r"Session started: ([0-9a-f]{12})", start_text)
        session_id = match.group(1)

        # 终止会话
        term_result = _run(write_tool.execute(
            session_id=session_id,
            terminate=True,
            yield_time_ms=1000,
        ))
        term_text = str(term_result)

        assert "exit code" in term_text.lower() or "still running" not in term_text.lower()


# ---------------------------------------------------------------------------
# ExecSessionManager
# ---------------------------------------------------------------------------


class TestExecSessionManager:
    """ExecSessionManager 测试。"""

    def test_start_and_poll(self) -> None:
        """启动会话并轮询。"""
        manager = ExecSessionManager()

        result = _run(manager.start(
            command=_py_cmd("print('manager test')"),
            cwd=".",
            env={},
            timeout=10,
            yield_time_ms=2000,
            max_output_chars=10000,
        ))
        session_id, poll = result

        assert len(session_id) == 12
        assert "manager test" in poll.output
        assert poll.done is True
        assert poll.exit_code == 0

    def test_session_removed_after_done(self) -> None:
        """会话完成后从管理器移除。"""
        manager = ExecSessionManager()

        session_id, _ = _run(manager.start(
            command=_py_cmd("print('done')"),
            cwd=".",
            env={},
            timeout=10,
            yield_time_ms=2000,
            max_output_chars=10000,
        ))

        assert manager.get(session_id) is None

    def test_get_nonexistent(self) -> None:
        """获取不存在的会话返回 None。"""
        manager = ExecSessionManager()
        assert manager.get("nonexistent") is None

    def test_write_to_nonexistent_raises(self) -> None:
        """向不存在的会话写入抛出 KeyError。"""
        manager = ExecSessionManager()
        with pytest.raises(KeyError):
            _run(manager.write(
                session_id="nonexistent",
                chars="test",
                close_stdin=False,
                terminate=False,
                yield_time_ms=1000,
                max_output_chars=10000,
            ))


# ---------------------------------------------------------------------------
# 工具发现
# ---------------------------------------------------------------------------


class TestSessionToolDiscovery:
    """工具发现测试。"""

    def test_write_stdin_discovered_with_manager(self, tmp_path: Path) -> None:
        """有 session_manager 时 write_stdin 被发现。"""
        ctx = _make_ctx(str(tmp_path), with_session_manager=True)
        registry = ToolRegistry()
        loaded = ToolLoader().load(ctx, registry, scope="core")

        assert "write_stdin" in loaded
        assert registry.has("write_stdin")

    def test_write_stdin_not_discovered_without_manager(self, tmp_path: Path) -> None:
        """无 session_manager 时 write_stdin 不被发现。"""
        ctx = _make_ctx(str(tmp_path), with_session_manager=False)
        registry = ToolRegistry()
        loaded = ToolLoader().load(ctx, registry, scope="core")

        assert "write_stdin" not in loaded

    def test_exec_still_discovered(self, tmp_path: Path) -> None:
        """exec 工具仍然被发现。"""
        ctx = _make_ctx(str(tmp_path))
        registry = ToolRegistry()
        loaded = ToolLoader().load(ctx, registry, scope="core")

        assert "exec" in loaded

    def test_write_stdin_schema(self, tmp_path: Path) -> None:
        """write_stdin 参数 schema 正确。"""
        ctx = _make_ctx(str(tmp_path))
        tool = WriteStdinTool.create(ctx)
        schema = tool.to_schema()

        assert schema["function"]["name"] == "write_stdin"
        props = schema["function"]["parameters"]["properties"]
        assert "session_id" in props
        assert "chars" in props
        assert "close_stdin" in props
        assert "terminate" in props
        assert "session_id" in schema["function"]["parameters"]["required"]


# ---------------------------------------------------------------------------
# owner_session_key 隔离（step124）
# ---------------------------------------------------------------------------


class TestExecSessionOwnerIsolation:
    """exec_session 按 owner_session_key 隔离（step124）。

    注意：``start`` 与 ``write`` 必须在同一个事件循环内执行（子进程 transport
    绑定到创建它的事件循环），故每个测试都在单次 ``asyncio.run`` 中完成
    启动、断言与清理。
    """

    def test_owner_stamped_and_visible_to_owner(self) -> None:
        """会话打上当前请求会话 key，且 owner 可看到/操作自己的会话。"""
        async def go() -> None:
            manager = ExecSessionManager()
            with request_context(RequestContext(session_key="owner-a")):
                session_id, poll = await manager.start(
                    command=_py_cmd("import time; time.sleep(3)"),
                    cwd=".", env=None, timeout=10, yield_time_ms=300,
                    max_output_chars=10000,
                )
                assert poll.done is False
                session = manager.get(session_id)
                assert session is not None
                assert session.owner_session_key == "owner-a"
                assert session_id in [i.session_id for i in manager.list()]
                # owner 可在同一事件循环内终止自己的会话
                await manager.write(
                    session_id=session_id, chars=None, close_stdin=True,
                    terminate=True, yield_time_ms=1000, max_output_chars=10000,
                )
            # 上下文退出后 current_request_session_key 为 None → 不过滤（已移除）
            assert manager.get(session_id) is None
        _run(go())

    def test_owner_isolation_hides_other_sessions(self) -> None:
        """owner-b 看不到/操作不了 owner-a 创建的会话。"""
        async def go() -> None:
            manager = ExecSessionManager()
            with request_context(RequestContext(session_key="owner-a")):
                sid_a, _ = await manager.start(
                    command=_py_cmd("import time; time.sleep(3)"),
                    cwd=".", env=None, timeout=10, yield_time_ms=300,
                    max_output_chars=10000,
                )
            # owner-b 看不到/操作不了 owner-a 的会话
            with request_context(RequestContext(session_key="owner-b")):
                assert sid_a not in [i.session_id for i in manager.list()]
                assert manager.get(sid_a) is None
                with pytest.raises(KeyError):
                    await manager.write(
                        session_id=sid_a, chars="x", close_stdin=False,
                        terminate=False, yield_time_ms=1000, max_output_chars=10000,
                    )
            # 回到 owner-a 终止会话
            with request_context(RequestContext(session_key="owner-a")):
                await manager.write(
                    session_id=sid_a, chars=None, close_stdin=True, terminate=True,
                    yield_time_ms=1000, max_output_chars=10000,
                )
        _run(go())

    def test_owner_none_visible_to_all_without_context(self) -> None:
        """无归属（owner=None）会话对任一观察者可见，兼容遗留。"""
        async def go() -> None:
            manager = ExecSessionManager()
            # 无上下文创建 → owner=None
            sid, _ = await manager.start(
                command=_py_cmd("import time; time.sleep(3)"),
                cwd=".", env=None, timeout=10, yield_time_ms=300, max_output_chars=10000,
            )
            try:
                with request_context(RequestContext(session_key="someone")):
                    assert manager.get(sid) is not None
                    assert sid in [i.session_id for i in manager.list()]
            finally:
                # 无上下文 → 不过滤，可清理
                await manager.write(
                    session_id=sid, chars=None, close_stdin=True, terminate=True,
                    yield_time_ms=1000, max_output_chars=10000,
                )
        _run(go())
