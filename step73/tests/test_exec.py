"""step69：ExecTool 基础版单元测试。

覆盖：
- 简单命令执行
- 非零退出码
- 超时
- 危险命令黑名单
- working_dir
- workspace 边界
- 输出截断
- stderr 输出
- 工具发现与 schema
- 配置禁用
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from step73.context import ToolContext
from step73.loader import ToolLoader
from step73.tool import ToolRegistry, ToolResult
from step73.tools.shell import ExecTool, _DEFAULT_DENY_PATTERNS


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _make_config(*, exec_enable: bool = True, exec_timeout: int = 60, restrict: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        exec=SimpleNamespace(enable=exec_enable, timeout=exec_timeout, sandbox=""),
        tools=SimpleNamespace(restrict_to_workspace=restrict),
    )


def _make_ctx(workspace: str, *, exec_enable: bool = True, exec_timeout: int = 60, restrict: bool = False) -> ToolContext:
    return ToolContext(
        config=_make_config(exec_enable=exec_enable, exec_timeout=exec_timeout, restrict=restrict),
        workspace=workspace,
        restrict_to_workspace=restrict,
        session_key="test-session",
    )


def _run(coro):
    return asyncio.run(coro)


def _py_cmd(code: str) -> str:
    """生成跨平台的 python -c 命令（路径含空格时加引号）。"""
    py = sys.executable
    if " " in py:
        py = f'"{py}"'
    return f'{py} -c "{code}"'


# ---------------------------------------------------------------------------
# 基础执行
# ---------------------------------------------------------------------------


class TestExecBasic:
    """基础命令执行。"""

    def test_echo_command(self, tmp_path: Path) -> None:
        """简单命令执行成功，返回输出。"""
        ctx = _make_ctx(str(tmp_path))
        tool = ExecTool.create(ctx)

        result = _run(tool.execute(command=_py_cmd("print('hello world')")))
        text = str(result)

        assert "hello world" in text
        assert "Exit code: 0" in text

    def test_nonzero_exit_code(self, tmp_path: Path) -> None:
        """非零退出码返回错误输出和退出码。"""
        ctx = _make_ctx(str(tmp_path))
        tool = ExecTool.create(ctx)

        result = _run(tool.execute(command=_py_cmd("import sys; sys.exit(42)")))
        text = str(result)

        assert "Exit code: 42" in text

    def test_no_output(self, tmp_path: Path) -> None:
        """无输出返回提示。"""
        ctx = _make_ctx(str(tmp_path))
        tool = ExecTool.create(ctx)

        result = _run(tool.execute(command=_py_cmd("pass")))
        text = str(result)

        assert "Exit code: 0" in text

    def test_missing_command(self, tmp_path: Path) -> None:
        """空 command 返回错误。"""
        ctx = _make_ctx(str(tmp_path))
        tool = ExecTool.create(ctx)

        result = _run(tool.execute(command=""))
        assert isinstance(result, ToolResult)
        assert result.is_error
        assert "Missing command" in str(result)

    def test_none_command(self, tmp_path: Path) -> None:
        """None command 返回错误。"""
        ctx = _make_ctx(str(tmp_path))
        tool = ExecTool.create(ctx)

        result = _run(tool.execute(command=None))
        assert isinstance(result, ToolResult)
        assert result.is_error


# ---------------------------------------------------------------------------
# 超时
# ---------------------------------------------------------------------------


class TestExecTimeout:
    """超时控制。"""

    def test_timeout_kills_process(self, tmp_path: Path) -> None:
        """超时命令被杀死并返回超时错误。"""
        ctx = _make_ctx(str(tmp_path), exec_timeout=60)
        tool = ExecTool.create(ctx)

        # sleep 10 秒，但超时设为 1 秒
        result = _run(tool.execute(
            command=_py_cmd("import time; time.sleep(10)"),
            timeout=1,
        ))
        assert isinstance(result, ToolResult)
        assert result.is_error
        assert "timed out" in str(result).lower()

    def test_custom_timeout(self, tmp_path: Path) -> None:
        """自定义超时生效（不超时的情况）。"""
        ctx = _make_ctx(str(tmp_path))
        tool = ExecTool.create(ctx)

        result = _run(tool.execute(
            command=_py_cmd("print('fast')"),
            timeout=10,
        ))
        assert "fast" in str(result)
        assert "Exit code: 0" in str(result)

    def test_timeout_capped_at_max(self, tmp_path: Path) -> None:
        """超时超过最大值时被截断。"""
        ctx = _make_ctx(str(tmp_path))
        tool = ExecTool.create(ctx)

        # _resolve_timeout 应该把 9999 截断到 600
        effective = tool._resolve_timeout(9999)
        assert effective == 600

    def test_timeout_zero_means_no_limit(self, tmp_path: Path) -> None:
        """timeout=0 表示不限制。"""
        ctx = _make_ctx(str(tmp_path))
        tool = ExecTool.create(ctx)

        effective = tool._resolve_timeout(0)
        assert effective is None


# ---------------------------------------------------------------------------
# 危险命令
# ---------------------------------------------------------------------------


class TestExecDangerous:
    """危险命令黑名单。"""

    def test_rm_rf_blocked(self, tmp_path: Path) -> None:
        """rm -rf 被拦截。"""
        ctx = _make_ctx(str(tmp_path))
        tool = ExecTool.create(ctx)

        result = _run(tool.execute(command="rm -rf /tmp/test"))
        assert isinstance(result, ToolResult)
        assert result.is_error
        assert "blocked" in str(result).lower()

    def test_format_blocked(self, tmp_path: Path) -> None:
        """format 被拦截。"""
        ctx = _make_ctx(str(tmp_path))
        tool = ExecTool.create(ctx)

        result = _run(tool.execute(command="format C:"))
        assert isinstance(result, ToolResult)
        assert result.is_error

    def test_shutdown_blocked(self, tmp_path: Path) -> None:
        """shutdown 被拦截。"""
        ctx = _make_ctx(str(tmp_path))
        tool = ExecTool.create(ctx)

        result = _run(tool.execute(command="shutdown -h now"))
        assert isinstance(result, ToolResult)
        assert result.is_error

    def test_reboot_blocked(self, tmp_path: Path) -> None:
        """reboot 被拦截。"""
        ctx = _make_ctx(str(tmp_path))
        tool = ExecTool.create(ctx)

        result = _run(tool.execute(command="reboot"))
        assert isinstance(result, ToolResult)
        assert result.is_error

    def test_safe_command_not_blocked(self, tmp_path: Path) -> None:
        """安全命令不被拦截。"""
        ctx = _make_ctx(str(tmp_path))
        tool = ExecTool.create(ctx)

        result = _run(tool.execute(command=_py_cmd("print('safe')")))
        assert "safe" in str(result)

    def test_rm_without_rf_not_blocked(self, tmp_path: Path) -> None:
        """普通 rm（不带 -rf）不被拦截。"""
        ctx = _make_ctx(str(tmp_path))
        tool = ExecTool.create(ctx)

        # rm 不带 -rf 应该通过过滤检查
        danger = tool._check_command_filter("rm file.txt")
        assert danger is None

    def test_default_deny_patterns_not_empty(self) -> None:
        """默认黑名单非空。"""
        assert len(_DEFAULT_DENY_PATTERNS) > 0


# ---------------------------------------------------------------------------
# working_dir
# ---------------------------------------------------------------------------


class TestExecWorkingDir:
    """工作目录。"""

    def test_working_dir(self, tmp_path: Path) -> None:
        """指定 working_dir 执行。"""
        subdir = tmp_path / "subdir"
        subdir.mkdir()

        ctx = _make_ctx(str(tmp_path))
        tool = ExecTool.create(ctx)

        result = _run(tool.execute(
            command=_py_cmd("import os; print(os.getcwd())"),
            working_dir=str(subdir),
        ))
        text = str(result)
        # 输出中应包含 subdir 路径
        assert "subdir" in text

    def test_default_working_dir_is_workspace(self, tmp_path: Path) -> None:
        """默认工作目录是 workspace。"""
        ctx = _make_ctx(str(tmp_path))
        tool = ExecTool.create(ctx)

        result = _run(tool.execute(command=_py_cmd("import os; print(os.getcwd())")))
        text = str(result)
        # 输出中应包含 tmp_path
        assert tmp_path.name in text


# ---------------------------------------------------------------------------
# workspace 边界
# ---------------------------------------------------------------------------


class TestExecWorkspaceBoundary:
    """workspace 边界检查。"""

    def test_outside_workspace_rejected(self, tmp_path: Path) -> None:
        """受限模式下越界 working_dir 被拒绝。"""
        outside = tmp_path.parent / "outside_workspace"
        outside.mkdir(exist_ok=True)

        ctx = _make_ctx(str(tmp_path), restrict=True)
        tool = ExecTool.create(ctx)

        result = _run(tool.execute(
            command=_py_cmd("print('test')"),
            working_dir=str(outside),
        ))
        assert isinstance(result, ToolResult)
        assert result.is_error
        assert "outside" in str(result).lower() or "workspace" in str(result).lower()

    def test_inside_workspace_allowed(self, tmp_path: Path) -> None:
        """受限模式下 workspace 内的 working_dir 被允许。"""
        subdir = tmp_path / "subdir"
        subdir.mkdir()

        ctx = _make_ctx(str(tmp_path), restrict=True)
        tool = ExecTool.create(ctx)

        result = _run(tool.execute(
            command=_py_cmd("print('inside')"),
            working_dir=str(subdir),
        ))
        assert "inside" in str(result)

    def test_unrestricted_allows_outside(self, tmp_path: Path) -> None:
        """非受限模式下越界 working_dir 被允许。"""
        outside = tmp_path.parent / "outside_unrestricted"
        outside.mkdir(exist_ok=True)

        ctx = _make_ctx(str(tmp_path), restrict=False)
        tool = ExecTool.create(ctx)

        result = _run(tool.execute(
            command=_py_cmd("print('outside ok')"),
            working_dir=str(outside),
        ))
        assert "outside ok" in str(result)


# ---------------------------------------------------------------------------
# 输出截断
# ---------------------------------------------------------------------------


class TestExecOutputTruncation:
    """输出截断。"""

    def test_long_output_truncated(self, tmp_path: Path) -> None:
        """长输出被截断。"""
        ctx = _make_ctx(str(tmp_path))
        tool = ExecTool.create(ctx)

        # 生成 20000 字符的输出
        result = _run(tool.execute(
            command=_py_cmd("print('x' * 20000)"),
        ))
        text = str(result)

        assert "truncated" in text.lower()
        # 截断后长度应远小于 20000
        assert len(text) < 15000

    def test_short_output_not_truncated(self, tmp_path: Path) -> None:
        """短输出不被截断。"""
        ctx = _make_ctx(str(tmp_path))
        tool = ExecTool.create(ctx)

        result = _run(tool.execute(command=_py_cmd("print('short')")))
        text = str(result)

        assert "truncated" not in text.lower()

    def test_truncate_preserves_head_and_tail(self) -> None:
        """截断保留头尾内容。"""
        tool = ExecTool()
        text = "A" * 100 + "B" * 100 + "C" * 100
        result = tool._truncate_output(text, 100)

        assert "A" * 50 in result  # 头部保留
        assert "C" * 50 in result  # 尾部保留
        assert "truncated" in result.lower()


# ---------------------------------------------------------------------------
# stderr
# ---------------------------------------------------------------------------


class TestExecStderr:
    """stderr 输出。"""

    def test_stderr_displayed(self, tmp_path: Path) -> None:
        """stderr 输出正确显示。"""
        ctx = _make_ctx(str(tmp_path))
        tool = ExecTool.create(ctx)

        result = _run(tool.execute(
            command=_py_cmd("import sys; sys.stderr.write('error message\\n')"),
        ))
        text = str(result)

        assert "STDERR" in text
        assert "error message" in text


# ---------------------------------------------------------------------------
# 工具发现与 schema
# ---------------------------------------------------------------------------


class TestExecDiscovery:
    """工具发现与 schema。"""

    def test_tool_discovered(self, tmp_path: Path) -> None:
        """ToolLoader 自动发现 exec 工具。"""
        ctx = _make_ctx(str(tmp_path))
        registry = ToolRegistry()
        loaded = ToolLoader().load(ctx, registry, scope="core")

        assert "exec" in loaded
        assert registry.has("exec")

    def test_tool_schema(self, tmp_path: Path) -> None:
        """参数 schema 正确。"""
        ctx = _make_ctx(str(tmp_path))
        tool = ExecTool.create(ctx)
        schema = tool.to_schema()

        assert schema["function"]["name"] == "exec"
        props = schema["function"]["parameters"]["properties"]
        assert "command" in props
        assert "working_dir" in props
        assert "timeout" in props
        assert "command" in schema["function"]["parameters"]["required"]

    def test_tool_not_read_only(self, tmp_path: Path) -> None:
        """exec 不是只读工具。"""
        ctx = _make_ctx(str(tmp_path))
        tool = ExecTool.create(ctx)
        assert tool.read_only is False

    def test_tool_name(self, tmp_path: Path) -> None:
        """工具名正确。"""
        ctx = _make_ctx(str(tmp_path))
        tool = ExecTool.create(ctx)
        assert tool.name == "exec"


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


class TestExecConfig:
    """配置集成。"""

    def test_config_disabled(self, tmp_path: Path) -> None:
        """config.exec.enable=False 时工具不被加载。"""
        ctx = _make_ctx(str(tmp_path), exec_enable=False)
        registry = ToolRegistry()
        loaded = ToolLoader().load(ctx, registry, scope="core")

        assert "exec" not in loaded
        assert not registry.has("exec")

    def test_config_enabled(self, tmp_path: Path) -> None:
        """config.exec.enable=True 时工具被加载。"""
        ctx = _make_ctx(str(tmp_path), exec_enable=True)
        registry = ToolRegistry()
        loaded = ToolLoader().load(ctx, registry, scope="core")

        assert "exec" in loaded

    def test_config_timeout_used(self, tmp_path: Path) -> None:
        """配置中的 timeout 被使用。"""
        ctx = _make_ctx(str(tmp_path), exec_timeout=30)
        tool = ExecTool.create(ctx)

        assert tool.timeout == 30

    def test_enabled_classmethod(self, tmp_path: Path) -> None:
        """enabled 类方法正确读取配置。"""
        ctx_enabled = _make_ctx(str(tmp_path), exec_enable=True)
        ctx_disabled = _make_ctx(str(tmp_path), exec_enable=False)

        assert ExecTool.enabled(ctx_enabled) is True
        assert ExecTool.enabled(ctx_disabled) is False
