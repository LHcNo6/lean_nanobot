"""step70：ExecTool 增强版单元测试。

覆盖：
- 环境变量白名单（_build_env）
- 灵活命令过滤（allow/deny/白名单模式）
- PATH 管理（path_prepend/path_append）
- 非零退出码标记
- 配置字段读取
- 向后兼容（step69 测试复用）
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from step125.context import ToolContext
from step125.tool import ToolResult
from step125.tools.shell import ExecTool, _DEFAULT_DENY_PATTERNS


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _make_config(
    *,
    exec_enable: bool = True,
    exec_timeout: int = 60,
    restrict: bool = False,
    allowed_env_keys: list[str] | None = None,
    allow_patterns: list[str] | None = None,
    deny_patterns: list[str] | None = None,
    path_prepend: str = "",
    path_append: str = "",
) -> SimpleNamespace:
    return SimpleNamespace(
        exec=SimpleNamespace(
            enable=exec_enable,
            timeout=exec_timeout,
            sandbox="",
            allowed_env_keys=allowed_env_keys or [],
            allow_patterns=allow_patterns or [],
            deny_patterns=deny_patterns or [],
            path_prepend=path_prepend,
            path_append=path_append,
        ),
        tools=SimpleNamespace(restrict_to_workspace=restrict),
    )


def _make_ctx(workspace: str, **kwargs) -> ToolContext:
    return ToolContext(
        config=_make_config(**kwargs),
        workspace=workspace,
        restrict_to_workspace=kwargs.get("restrict", False),
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
# 环境变量白名单
# ---------------------------------------------------------------------------


class TestBuildEnv:
    """_build_env 环境变量管理。"""

    def test_build_env_minimal_unix_like(self) -> None:
        """_build_env 返回最小化环境（不包含任意变量）。"""
        tool = ExecTool()
        env = tool._build_env()

        # 必须包含基础变量
        assert "PYTHONUNBUFFERED" in env
        # 不应包含任意测试变量
        assert "MY_RANDOM_TEST_VAR" not in env

    def test_allowed_env_keys_passed(self, monkeypatch) -> None:
        """白名单中的环境变量被传递。"""
        monkeypatch.setenv("MY_ALLOWED_VAR", "secret_value")
        monkeypatch.setenv("MY_DENIED_VAR", "should_not_pass")

        tool = ExecTool(allowed_env_keys=["MY_ALLOWED_VAR"])
        env = tool._build_env()

        assert env.get("MY_ALLOWED_VAR") == "secret_value"
        assert "MY_DENIED_VAR" not in env

    def test_allowed_env_keys_missing_not_added(self, monkeypatch) -> None:
        """白名单中不存在的变量不被添加。"""
        monkeypatch.delenv("MY_MISSING_VAR", raising=False)

        tool = ExecTool(allowed_env_keys=["MY_MISSING_VAR"])
        env = tool._build_env()

        assert "MY_MISSING_VAR" not in env

    def test_empty_allowed_env_keys(self) -> None:
        """空白名单不添加额外变量。"""
        tool = ExecTool(allowed_env_keys=[])
        env = tool._build_env()
        # 只包含基础变量
        assert len(env) < 30  # Windows 基础变量约 17 个，Unix 约 4 个


# ---------------------------------------------------------------------------
# 灵活命令过滤
# ---------------------------------------------------------------------------


class TestCommandFilter:
    """_check_command_filter 灵活命令过滤。"""

    def test_default_deny_blocks_rm_rf(self) -> None:
        """默认黑名单拦截 rm -rf。"""
        tool = ExecTool()
        result = tool._check_command_filter("rm -rf /tmp/test")
        assert result is not None
        assert "deny pattern" in result.lower()

    def test_default_deny_blocks_format(self) -> None:
        """默认黑名单拦截 format。"""
        tool = ExecTool()
        result = tool._check_command_filter("format C:")
        assert result is not None

    def test_safe_command_passes(self) -> None:
        """安全命令通过过滤。"""
        tool = ExecTool()
        result = tool._check_command_filter("echo hello")
        assert result is None

    def test_custom_deny_pattern(self) -> None:
        """自定义 deny 模式拦截。"""
        tool = ExecTool(deny_patterns=[r"\bnpm\s+publish\b"])
        result = tool._check_command_filter("npm publish")
        assert result is not None
        assert "npm" in result.lower() or "deny" in result.lower()

    def test_custom_deny_does_not_block_others(self) -> None:
        """自定义 deny 不影响其他命令。"""
        tool = ExecTool(deny_patterns=[r"\bnpm\s+publish\b"])
        result = tool._check_command_filter("npm install")
        assert result is None

    def test_allow_overrides_deny(self) -> None:
        """allow 模式豁免 deny 拦截。"""
        tool = ExecTool(
            allow_patterns=[r"rm -rf ./build"],
            deny_patterns=[],  # 不额外添加，但默认黑名单仍在
        )
        # allow 匹配 → 直接允许，跳过默认 deny 检查
        result = tool._check_command_filter("rm -rf ./build")
        assert result is None

    def test_allowlist_mode_rejects_unknown(self) -> None:
        """白名单模式拒绝不在 allow 中的命令。"""
        tool = ExecTool(allow_patterns=[r"echo .*", r"ls"])
        result = tool._check_command_filter("rm file.txt")
        assert result is not None
        assert "allowlist" in result.lower()

    def test_allowlist_mode_allows_matching(self) -> None:
        """白名单模式允许匹配的命令。"""
        tool = ExecTool(allow_patterns=[r"echo .*"])
        result = tool._check_command_filter("echo hello world")
        assert result is None

    def test_allow_uses_fullmatch(self) -> None:
        """allow 使用 fullmatch（不是 search）。"""
        tool = ExecTool(allow_patterns=[r"echo"])
        # "echo hello" 不 fullmatch "echo" → 不被 allow 豁免
        result = tool._check_command_filter("echo hello")
        # 不在 allow 中，且不匹配 deny → 白名单模式拒绝
        assert result is not None
        assert "allowlist" in result.lower()

    def test_no_allow_no_deny_extra(self) -> None:
        """无 allow 无额外 deny 时，仅默认黑名单生效。"""
        tool = ExecTool()
        assert tool._check_command_filter("echo test") is None
        assert tool._check_command_filter("rm -rf /") is not None

    def test_deny_patterns_includes_default(self) -> None:
        """deny_patterns 包含默认黑名单。"""
        tool = ExecTool(deny_patterns=[r"custom"])
        # 默认黑名单 + 1 个自定义
        assert len(tool.deny_patterns) == len(_DEFAULT_DENY_PATTERNS) + 1


# ---------------------------------------------------------------------------
# PATH 管理
# ---------------------------------------------------------------------------


class TestPathManagement:
    """_apply_path PATH 管理。"""

    def test_no_path_modification(self) -> None:
        """无 path_prepend/path_append 时不修改。"""
        tool = ExecTool()
        env = {"PATH": "/usr/bin"}
        new_env, new_cmd = tool._apply_path(env, "echo test")
        assert new_env["PATH"] == "/usr/bin"
        assert new_cmd == "echo test"

    def test_path_prepend_windows(self, monkeypatch) -> None:
        """Windows 下 path_prepend 修改 env[PATH]。"""
        monkeypatch.setattr("step73.tools.shell._IS_WINDOWS", True)
        tool = ExecTool(path_prepend="C:\\tools")
        env = {"PATH": "C:\\Windows"}
        new_env, new_cmd = tool._apply_path(env, "echo test")
        assert new_env["PATH"].startswith("C:\\tools")
        assert "C:\\Windows" in new_env["PATH"]
        assert new_cmd == "echo test"  # 命令不变

    def test_path_append_windows(self, monkeypatch) -> None:
        """Windows 下 path_append 追加到 env[PATH]。"""
        monkeypatch.setattr("step73.tools.shell._IS_WINDOWS", True)
        tool = ExecTool(path_append="C:\\extra")
        env = {"PATH": "C:\\Windows"}
        new_env, _ = tool._apply_path(env, "echo test")
        assert new_env["PATH"].endswith("C:\\extra")

    def test_path_prepend_unix(self, monkeypatch) -> None:
        """Unix 下 path_prepend 在命令前加 export。"""
        monkeypatch.setattr("step73.tools.shell._IS_WINDOWS", False)
        tool = ExecTool(path_prepend="/opt/tools")
        env = {"PATH": "/usr/bin"}
        new_env, new_cmd = tool._apply_path(env, "echo test")
        assert "export PATH=" in new_cmd
        assert "/opt/tools" in new_cmd
        assert "echo test" in new_cmd

    def test_both_prepend_and_append(self, monkeypatch) -> None:
        """同时设置 prepend 和 append。"""
        monkeypatch.setattr("step73.tools.shell._IS_WINDOWS", True)
        tool = ExecTool(path_prepend="/pre", path_append="/post")
        env = {"PATH": "/mid"}
        new_env, _ = tool._apply_path(env, "cmd")
        assert new_env["PATH"].startswith("/pre")
        assert new_env["PATH"].endswith("/post")
        assert "/mid" in new_env["PATH"]


# ---------------------------------------------------------------------------
# 非零退出码标记
# ---------------------------------------------------------------------------


class TestExitCodeMarker:
    """非零退出码标记。"""

    def test_nonzero_exit_code_marker(self, tmp_path: Path) -> None:
        """非零退出码输出带 [exit code N] 前缀。"""
        ctx = _make_ctx(str(tmp_path))
        tool = ExecTool.create(ctx)

        result = _run(tool.execute(command=_py_cmd("import sys; sys.exit(42)")))
        text = str(result)

        assert text.startswith("[exit code 42]")

    def test_zero_exit_code_no_marker(self, tmp_path: Path) -> None:
        """零退出码不带前缀。"""
        ctx = _make_ctx(str(tmp_path))
        tool = ExecTool.create(ctx)

        result = _run(tool.execute(command=_py_cmd("print('ok')")))
        text = str(result)

        assert not text.startswith("[exit code")
        assert "Exit code: 0" in text

    def test_exit_code_still_in_output(self, tmp_path: Path) -> None:
        """退出码仍在输出末尾显示。"""
        ctx = _make_ctx(str(tmp_path))
        tool = ExecTool.create(ctx)

        result = _run(tool.execute(command=_py_cmd("import sys; sys.exit(7)")))
        text = str(result)

        assert "[exit code 7]" in text  # 前缀
        assert "Exit code: 7" in text    # 末尾


# ---------------------------------------------------------------------------
# 配置字段读取
# ---------------------------------------------------------------------------


class TestConfigFields:
    """配置字段正确读取。"""

    def test_create_reads_all_fields(self, tmp_path: Path) -> None:
        """create 从配置读取所有增强字段。"""
        ctx = _make_ctx(
            str(tmp_path),
            allowed_env_keys=["MY_VAR"],
            allow_patterns=[r"echo .*"],
            deny_patterns=[r"custom_deny"],
            path_prepend="/pre",
            path_append="/post",
        )
        tool = ExecTool.create(ctx)

        assert tool.allowed_env_keys == ["MY_VAR"]
        assert tool.allow_patterns == [r"echo .*"]
        assert "custom_deny" in tool.deny_patterns
        assert tool.path_prepend == "/pre"
        assert tool.path_append == "/post"

    def test_default_config_values(self, tmp_path: Path) -> None:
        """默认配置值正确。"""
        ctx = _make_ctx(str(tmp_path))
        tool = ExecTool.create(ctx)

        assert tool.allowed_env_keys == []
        assert tool.allow_patterns == []
        assert tool.deny_patterns == list(_DEFAULT_DENY_PATTERNS)
        assert tool.path_prepend == ""
        assert tool.path_append == ""


# ---------------------------------------------------------------------------
# 向后兼容（step69 核心行为）
# ---------------------------------------------------------------------------


class TestBackwardCompatibility:
    """step69 核心行为在 step70 中保持。"""

    def test_echo_command(self, tmp_path: Path) -> None:
        """简单命令执行成功。"""
        ctx = _make_ctx(str(tmp_path))
        tool = ExecTool.create(ctx)
        result = _run(tool.execute(command=_py_cmd("print('hello')")))
        assert "hello" in str(result)

    def test_timeout(self, tmp_path: Path) -> None:
        """超时命令被杀死。"""
        ctx = _make_ctx(str(tmp_path))
        tool = ExecTool.create(ctx)
        result = _run(tool.execute(
            command=_py_cmd("import time; time.sleep(10)"),
            timeout=1,
        ))
        assert isinstance(result, ToolResult) and result.is_error

    def test_dangerous_blocked(self, tmp_path: Path) -> None:
        """危险命令被拦截。"""
        ctx = _make_ctx(str(tmp_path))
        tool = ExecTool.create(ctx)
        result = _run(tool.execute(command="rm -rf /tmp/test"))
        assert isinstance(result, ToolResult) and result.is_error

    def test_workspace_boundary(self, tmp_path: Path) -> None:
        """受限模式下越界 working_dir 被拒绝。"""
        outside = tmp_path.parent / "outside_bc"
        outside.mkdir(exist_ok=True)
        ctx = _make_ctx(str(tmp_path), restrict=True)
        tool = ExecTool.create(ctx)
        result = _run(tool.execute(
            command=_py_cmd("print('test')"),
            working_dir=str(outside),
        ))
        assert isinstance(result, ToolResult) and result.is_error

    def test_output_truncation(self, tmp_path: Path) -> None:
        """长输出被截断。"""
        ctx = _make_ctx(str(tmp_path))
        tool = ExecTool.create(ctx)
        result = _run(tool.execute(command=_py_cmd("print('x' * 20000)")))
        assert "truncated" in str(result).lower()

    def test_stderr_displayed(self, tmp_path: Path) -> None:
        """stderr 正确显示。"""
        ctx = _make_ctx(str(tmp_path))
        tool = ExecTool.create(ctx)
        result = _run(tool.execute(
            command=_py_cmd("import sys; sys.stderr.write('err\\n')"),
        ))
        assert "STDERR" in str(result)
