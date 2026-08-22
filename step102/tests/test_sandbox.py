"""step89：ExecTool 沙箱后端单元测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from step102.tools.sandbox import (
    _BACKENDS,
    _bwrap,
    _none,
    available_backends,
    wrap_command,
)


class TestNoneBackend:
    """none 后端。"""

    def test_returns_original_command(self) -> None:
        """返回原命令。"""
        result = _none("ls -la", "/workspace", "/workspace/sub")
        assert result == "ls -la"

    def test_wrap_command_none(self) -> None:
        """wrap_command with 'none'。"""
        result = wrap_command("none", "echo hello", "/ws", "/ws")
        assert result == "echo hello"

    def test_wrap_command_empty_string(self) -> None:
        """空字符串等同于 none。"""
        result = wrap_command("", "echo hello", "/ws", "/ws")
        assert result == "echo hello"


class TestBwrapBackend:
    """bwrap 后端。"""

    def test_contains_bwrap(self) -> None:
        """命令以 bwrap 开头。"""
        result = _bwrap("ls", "/workspace", "/workspace")
        assert result.startswith("bwrap ")

    def test_contains_workspace_bind(self, tmp_path: Path) -> None:
        """包含 workspace 读写绑定。"""
        ws = str(tmp_path)
        result = _bwrap("ls", ws, ws)
        assert "--bind" in result
        assert tmp_path.name in result

    def test_contains_proc_dev_tmpfs(self) -> None:
        """包含 /proc, /dev, /tmp。"""
        result = _bwrap("ls", "/ws", "/ws")
        assert "--proc" in result
        assert "/proc" in result
        assert "--dev" in result
        assert "/dev" in result
        assert "--tmpfs" in result
        assert "/tmp" in result

    def test_contains_ro_bind_system(self) -> None:
        """包含系统目录只读绑定。"""
        result = _bwrap("ls", "/ws", "/ws")
        assert "--ro-bind" in result
        assert "/usr" in result

    def test_contains_sh_c_command(self) -> None:
        """包含 sh -c 和原命令。"""
        result = _bwrap("echo hello", "/ws", "/ws")
        assert "sh" in result
        assert "-c" in result
        assert "echo hello" in result

    def test_chdir_to_cwd(self) -> None:
        """包含 --chdir 到 cwd。"""
        result = _bwrap("ls", "/ws", "/ws/subdir")
        assert "--chdir" in result
        assert "subdir" in result

    def test_wrap_command_bwrap(self) -> None:
        """wrap_command with 'bwrap'。"""
        result = wrap_command("bwrap", "ls", "/ws", "/ws")
        assert result.startswith("bwrap ")


class TestWrapCommand:
    """wrap_command 入口函数。"""

    def test_unknown_sandbox_raises(self) -> None:
        """未知沙箱名报错。"""
        with pytest.raises(ValueError, match="Unknown sandbox"):
            wrap_command("docker", "ls", "/ws", "/ws")

    def test_unknown_sandbox_lists_available(self) -> None:
        """错误信息包含可用后端列表。"""
        with pytest.raises(ValueError) as exc_info:
            wrap_command("invalid", "ls", "/ws", "/ws")
        assert "bwrap" in str(exc_info.value)
        assert "none" in str(exc_info.value)

    def test_available_backends(self) -> None:
        """available_backends 返回列表。"""
        backends = available_backends()
        assert "none" in backends
        assert "bwrap" in backends

    def test_backends_registered(self) -> None:
        """_BACKENDS 注册了后端。"""
        assert "none" in _BACKENDS
        assert "bwrap" in _BACKENDS
        assert callable(_BACKENDS["none"])
        assert callable(_BACKENDS["bwrap"])


class TestExecToolSandboxIntegration:
    """ExecTool 沙箱集成。"""

    def _make_config(self, sandbox: str = "") -> SimpleNamespace:
        return SimpleNamespace(
            exec=SimpleNamespace(
                enable=True, timeout=60, sandbox=sandbox,
                allowed_env_keys=[], allow_patterns=[], deny_patterns=[],
                path_prepend="", path_append="",
            ),
            tools=SimpleNamespace(restrict_to_workspace=False, file=SimpleNamespace(enable=True)),
            web=SimpleNamespace(enable=True, timeout=30, user_agent="Test", search=SimpleNamespace(provider="duckduckgo", max_results=5, timeout=30)),
            my=SimpleNamespace(enable=True, allow_set=False),
            image_generation=SimpleNamespace(enabled=True, provider="simple", save_dir="generated"),
        )

    def _make_ctx(self, workspace: str, sandbox: str = ""):
        from step102.context import ToolContext
        from step102.tools.file_state import FileStateStore
        from step102.tools.cron import _CronStore
        return ToolContext(
            config=self._make_config(sandbox),
            workspace=workspace,
            restrict_to_workspace=False,
            session_key="test",
            file_state_store=FileStateStore(),
            cron_store=_CronStore(),
        )

    def test_no_sandbox_does_not_wrap(self, tmp_path: Path) -> None:
        """sandbox="" 时不调用 wrap_command。"""
        from step102.tools.shell import ExecTool
        ctx = self._make_ctx(str(tmp_path), sandbox="")
        tool = ExecTool.create(ctx)

        with patch("step89.tools.shell.wrap_command") as mock_wrap:
            # mock create_subprocess_shell 避免实际执行
            with patch("asyncio.create_subprocess_shell") as mock_subproc:
                mock_process = type("P", (), {"communicate": AsyncMock(return_value=(b"", b"")), "returncode": 0, "pid": 1})()
                mock_subproc.return_value = mock_process
                asyncio.run(tool.execute(command="echo hello"))

            mock_wrap.assert_not_called()

    def test_bwrap_sandbox_wraps_command(self, tmp_path: Path) -> None:
        """sandbox='bwrap' 时调用 wrap_command。"""
        from step102.tools.shell import ExecTool
        ctx = self._make_ctx(str(tmp_path), sandbox="bwrap")
        tool = ExecTool.create(ctx)

        with patch("step89.tools.shell.wrap_command", return_value="bwrap ... echo hello") as mock_wrap:
            with patch("asyncio.create_subprocess_shell") as mock_subproc:
                mock_process = type("P", (), {"communicate": AsyncMock(return_value=(b"", b"")), "returncode": 0, "pid": 1})()
                mock_subproc.return_value = mock_process
                asyncio.run(tool.execute(command="echo hello"))

            mock_wrap.assert_called_once_with("bwrap", "echo hello", str(tmp_path), str(tmp_path))

    def test_invalid_sandbox_returns_error(self, tmp_path: Path) -> None:
        """无效沙箱名返回错误。"""
        from step102.tools.shell import ExecTool
        from step102.tool import ToolResult
        ctx = self._make_ctx(str(tmp_path), sandbox="invalid")
        tool = ExecTool.create(ctx)

        result = asyncio.run(tool.execute(command="echo hello"))
        assert isinstance(result, ToolResult)
        assert result.is_error
        assert "Unknown sandbox" in str(result)


# 辅助：AsyncMock
from unittest.mock import AsyncMock
