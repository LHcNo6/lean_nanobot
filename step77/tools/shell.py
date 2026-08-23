"""Shell 执行工具：ExecTool 增强版（step70）。

在 step69 基础版上增强：
- 环境变量白名单管理（_build_env + allowed_env_keys）；
- 灵活命令过滤（allow_patterns 优先 + deny_patterns）；
- PATH 管理（path_prepend / path_append）；
- 非零退出码标记。

对齐 nanobot `agent/tools/shell.py` 的环境变量和命令过滤子集，
简化了 shell 选择、login shell、沙箱、内部 URL 检测、绝对路径检查等。
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path
from typing import Any

from step77.schema import IntegerSchema, StringSchema, tool_parameters_schema
from step77.security.workspace_access import current_tool_workspace
from step77.security.workspace_policy import is_path_within
from step77.tool import Tool, ToolResult, tool_parameters
from step77.tools.exec_session import (
    DEFAULT_MAX_OUTPUT_CHARS,
    DEFAULT_YIELD_MS,
    MAX_OUTPUT_CHARS,
    MAX_YIELD_MS,
    _format_session_poll,
)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

_IS_WINDOWS = sys.platform == "win32"

# 默认危险命令黑名单（step69 沿用）
_DEFAULT_DENY_PATTERNS: list[str] = [
    r"\brm\s+-[rf]{1,2}\b",          # rm -r, rm -rf, rm -fr
    r"\bformat\b",                      # format
    r"\b(mkfs|diskpart)\b",            # 磁盘操作
    r"\bdd\s+if=",                      # dd if=
    r">\s*/dev/sd",                     # 写磁盘设备
    r"\b(shutdown|reboot|poweroff)\b", # 系统电源
    r":\(\)\s*\{.*\};\s*:",             # fork bomb
]


# ---------------------------------------------------------------------------
# ExecTool
# ---------------------------------------------------------------------------


@tool_parameters(
    tool_parameters_schema(
        command=StringSchema("The shell command to execute"),
        working_dir=StringSchema("Optional working directory for the command"),
        timeout=IntegerSchema(
            "Timeout in seconds (default 60, max 600). 0 = no limit.",
            minimum=0,
            maximum=600,
        ),
        yield_time_ms=IntegerSchema(
            "Optional: wait ms then return session_id for long-running commands (step73).",
            minimum=0,
            maximum=30000,
        ),
        max_output_chars=IntegerSchema(
            "Max output chars when yield_time_ms is used (default 10000).",
            minimum=1000,
            maximum=50000,
        ),
        required=["command"],
    )
)
class ExecTool(Tool):
    """Shell 命令执行工具（增强版）。

    step70 增强：
    - 环境变量白名单：最小化环境，allowed_env_keys 控制额外变量；
    - 灵活命令过滤：allow_patterns（优先）+ deny_patterns（可配置）；
    - PATH 管理：path_prepend / path_append 修改子进程 PATH；
    - 非零退出码标记：输出前缀 [exit code N]。
    """

    _scopes = {"core", "subagent"}
    config_key = "exec"

    _MAX_TIMEOUT = 600       # 单次最大超时（秒）
    _MAX_OUTPUT = 10_000     # 默认最大输出字符数

    # ------------------------------------------------------------------
    # 配置与创建
    # ------------------------------------------------------------------

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        """是否启用：读取 ``config.exec.enable``。"""
        return getattr(ctx.config.exec, "enable", True)

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        """从上下文创建工具实例。

        step70 新增读取：allowed_env_keys / allow_patterns / deny_patterns /
        path_prepend / path_append。
        """
        cfg = ctx.config.exec
        return cls(
            working_dir=ctx.workspace,
            timeout=getattr(cfg, "timeout", 60),
            restrict_to_workspace=getattr(ctx.config.tools, "restrict_to_workspace", False),
            allowed_env_keys=getattr(cfg, "allowed_env_keys", None),
            allow_patterns=getattr(cfg, "allow_patterns", None),
            deny_patterns=getattr(cfg, "deny_patterns", None),
            path_prepend=getattr(cfg, "path_prepend", ""),
            path_append=getattr(cfg, "path_append", ""),
            session_manager=getattr(ctx, "exec_session_manager", None),
        )

    def __init__(
        self,
        timeout: int = 60,
        working_dir: str | None = None,
        restrict_to_workspace: bool = False,
        deny_patterns: list[str] | None = None,
        allowed_env_keys: list[str] | None = None,
        allow_patterns: list[str] | None = None,
        path_prepend: str = "",
        path_append: str = "",
        session_manager: Any = None,
    ):
        """初始化 ExecTool（增强版 + step73 会话支持）。

        Args:
            timeout: 默认超时秒数（0=不限制）。
            working_dir: 默认工作目录。
            restrict_to_workspace: 是否限制在 workspace 内。
            deny_patterns: 额外危险命令正则（None 时仅用默认黑名单）。
            allowed_env_keys: 允许传递的额外环境变量名列表。
            allow_patterns: 命令允许正则（fullmatch，优先于 deny）。
            path_prepend: PATH 前缀。
            path_append: PATH 后缀。
            session_manager: ExecSessionManager 实例（step73，用于长运行命令）。
        """
        self.timeout = timeout
        self.working_dir = working_dir
        self.restrict_to_workspace = restrict_to_workspace
        # deny = 默认黑名单 + 配置中的额外 deny
        self.deny_patterns = list(_DEFAULT_DENY_PATTERNS)
        if deny_patterns:
            self.deny_patterns.extend(deny_patterns)
        self.allowed_env_keys = allowed_env_keys or []
        self.allow_patterns = allow_patterns or []
        self.path_prepend = path_prepend
        self.path_append = path_append
        self._session_manager = session_manager  # step73：长运行命令会话管理器

    # ------------------------------------------------------------------
    # 工具元信息
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        """工具名：``exec``。"""
        return "exec"

    @property
    def description(self) -> str:
        """工具描述。"""
        return (
            "Execute a shell command and return its output. "
            "Use this for tests, builds, package commands, git commands, and "
            "other process execution. Prefer read_file/find_files/grep for "
            "inspection and write_file/edit_file for file changes instead of "
            "cat, shell find/grep, echo, or sed. "
            "Output is truncated at 10,000 chars; timeout defaults to 60s."
        )

    @property
    def read_only(self) -> bool:
        """exec 不是只读操作。"""
        return False

    # ------------------------------------------------------------------
    # 主执行方法
    # ------------------------------------------------------------------

    async def execute(
        self,
        command: str | None = None,
        working_dir: str | None = None,
        timeout: int | None = None,
        yield_time_ms: int | None = None,
        max_output_chars: int | None = None,
        **kwargs: Any,
    ) -> str | ToolResult:
        """执行 shell 命令（增强版 + step73 会话模式）。

        step70 变化：
        - 使用 ``_build_env()`` 构建最小化环境；
        - 使用 ``_check_command_filter()`` 做灵活命令过滤；
        - 应用 PATH 修改；
        - 非零退出码加前缀标记。

        step73 变化：
        - ``yield_time_ms`` 不为 None 时启动会话模式，返回 session_id + 输出。
        """
        # 1. 参数校验
        if not command:
            return ToolResult.error("Error: Missing command.")

        # 2. 灵活命令过滤（allow 优先 + deny + 白名单模式）
        filter_error = self._check_command_filter(command)
        if filter_error:
            return ToolResult.error(filter_error)

        # 3. 解析工作目录
        cwd = self._resolve_cwd(working_dir)

        # 4. workspace 边界检查
        if self.restrict_to_workspace:
            boundary_error = self._check_workspace_boundary(cwd)
            if boundary_error:
                return ToolResult.error(boundary_error)

        # 5. 解析超时
        effective_timeout = self._resolve_timeout(timeout)

        # 6. 构建环境变量 + 应用 PATH 修改
        env = self._build_env()
        env, command = self._apply_path(env, command)

        # step73：会话模式（yield_time_ms 不为 None）
        if yield_time_ms is not None:
            return await self._execute_session(
                command, cwd, env, effective_timeout, yield_time_ms, max_output_chars
            )

        # 7-11. 创建子进程、等待、组装输出、截断、返回
        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_shell(
                command,
                cwd=cwd,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=effective_timeout,
                )
            except asyncio.TimeoutError:
                await self._kill_process(process)
                return ToolResult.error(
                    f"Error: Command timed out after {effective_timeout} seconds"
                )
            except asyncio.CancelledError:
                await self._kill_process(process)
                raise

            # 8. 组装输出
            output_parts: list[str] = []

            if stdout:
                output_parts.append(stdout.decode("utf-8", errors="replace"))

            if stderr:
                stderr_text = stderr.decode("utf-8", errors="replace")
                if stderr_text.strip():
                    output_parts.append(f"STDERR:\n{stderr_text}")

            exit_code = process.returncode if process.returncode is not None else -1
            output_parts.append(f"\nExit code: {exit_code}")

            # step70 新增：非零退出码前缀标记
            if exit_code != 0:
                output_parts.insert(0, f"[exit code {exit_code}]")

            result = "\n".join(output_parts) if output_parts else "(no output)"

            # 9. 输出截断
            result = self._truncate_output(result, self._MAX_OUTPUT)

            return result

        except Exception as exc:
            if process is not None:
                await self._kill_process(process)
            return ToolResult.error(f"Error executing command: {exc}")

    # ------------------------------------------------------------------
    # step70 新增：环境变量管理
    # ------------------------------------------------------------------

    def _build_env(self) -> dict[str, str]:
        """构建最小化环境变量字典。

        - Windows：传递系统必需变量（SYSTEMROOT/COMSPEC/PATH/TEMP 等）+ allowed_env_keys；
        - Unix：仅传递 HOME/LANG/TERM/PYTHONUNBUFFERED + allowed_env_keys。

        目的：最小化环境变量，减少敏感信息（如 API key）泄露给子进程。

        Returns:
            环境变量字典。
        """
        if _IS_WINDOWS:
            sr = os.environ.get("SYSTEMROOT", r"C:\Windows")
            env = {
                "SYSTEMROOT": sr,
                "COMSPEC": os.environ.get("COMSPEC", f"{sr}\\system32\\cmd.exe"),
                "USERPROFILE": os.environ.get("USERPROFILE", ""),
                "HOMEDRIVE": os.environ.get("HOMEDRIVE", "C:"),
                "HOMEPATH": os.environ.get("HOMEPATH", "\\"),
                "TEMP": os.environ.get("TEMP", f"{sr}\\Temp"),
                "TMP": os.environ.get("TMP", f"{sr}\\Temp"),
                "PATHEXT": os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD"),
                "PATH": os.environ.get("PATH", f"{sr}\\system32;{sr}"),
                "PYTHONUNBUFFERED": "1",
                "APPDATA": os.environ.get("APPDATA", ""),
                "LOCALAPPDATA": os.environ.get("LOCALAPPDATA", ""),
                "ProgramData": os.environ.get("ProgramData", ""),
                "ProgramFiles": os.environ.get("ProgramFiles", ""),
                "ProgramFiles(x86)": os.environ.get("ProgramFiles(x86)", ""),
                "ProgramW6432": os.environ.get("ProgramW6432", ""),
            }
        else:
            env = {
                "HOME": os.environ.get("HOME", "/tmp"),
                "LANG": os.environ.get("LANG", "C.UTF-8"),
                "TERM": os.environ.get("TERM", "dumb"),
                "PYTHONUNBUFFERED": "1",
            }

        # 白名单：允许传递的额外环境变量
        for key in self.allowed_env_keys:
            val = os.environ.get(key)
            if val is not None:
                env[key] = val

        return env

    # ------------------------------------------------------------------
    # step70 新增：灵活命令过滤
    # ------------------------------------------------------------------

    def _check_command_filter(self, command: str) -> str | None:
        """灵活命令过滤：allow 优先 + deny + 白名单模式。

        过滤逻辑：
        1. allow_patterns 非空且命令匹配任意 allow → 允许（跳过 deny）；
        2. 命令匹配任意 deny → 拒绝；
        3. allow_patterns 非空但命令不匹配任意 allow → 拒绝（白名单模式）；
        4. 否则 → 允许。

        Args:
            command: 要检查的命令字符串。

        Returns:
            被拒绝时返回错误消息，允许时返回 None。
        """
        lower = command.strip().lower()

        # 1. allow 优先：匹配 allow 则直接允许
        if self.allow_patterns:
            if any(re.fullmatch(p, lower) for p in self.allow_patterns):
                return None  # 显式允许

        # 2. deny 检查
        for pattern in self.deny_patterns:
            if re.search(pattern, lower):
                return f"Error: Command blocked by deny pattern: '{pattern}'."

        # 3. 白名单模式：allow 非空但不匹配 → 拒绝
        if self.allow_patterns:
            return "Error: Command blocked by allowlist filter (not in allowlist)."

        return None

    # ------------------------------------------------------------------
    # step70 新增：PATH 管理
    # ------------------------------------------------------------------

    def _apply_path(self, env: dict[str, str], command: str) -> tuple[dict[str, str], str]:
        """应用 PATH 修改（path_prepend / path_append）。

        - Windows：直接修改 env["PATH"]；
        - Unix：在命令前加 ``export PATH="..."`` 前缀。

        Args:
            env: 环境变量字典。
            command: 原始命令。

        Returns:
            (修改后的 env, 修改后的 command)。
        """
        if not self.path_prepend and not self.path_append:
            return env, command

        if _IS_WINDOWS:
            parts: list[str] = []
            if self.path_prepend:
                parts.append(self.path_prepend)
            if env.get("PATH"):
                parts.append(env["PATH"])
            if self.path_append:
                parts.append(self.path_append)
            env["PATH"] = os.pathsep.join(parts)
            return env, command
        else:
            segments: list[str] = []
            if self.path_prepend:
                segments.append(self.path_prepend)
            segments.append("$PATH")
            if self.path_append:
                segments.append(self.path_append)
            path_expr = os.pathsep.join(segments)
            return env, f'export PATH="{path_expr}"; {command}'

    # ------------------------------------------------------------------
    # step73：会话模式
    # ------------------------------------------------------------------

    async def _execute_session(
        self,
        command: str,
        cwd: str,
        env: dict[str, str],
        timeout: int | None,
        yield_time_ms: int,
        max_output_chars: int | None,
    ) -> str | ToolResult:
        """启动长运行命令会话。

        Args:
            command: 命令。
            cwd: 工作目录。
            env: 环境变量。
            timeout: 超时秒数。
            yield_time_ms: 首次轮询等待毫秒。
            max_output_chars: 最大输出字符数。

        Returns:
            格式化的会话输出文本，或错误。
        """
        if self._session_manager is None:
            return ToolResult.error(
                "Error: exec session manager not available. "
                "yield_time_ms requires an ExecSessionManager in ToolContext."
            )

        effective_yield = max(0, min(yield_time_ms, MAX_YIELD_MS))
        if effective_yield == 0:
            effective_yield = DEFAULT_YIELD_MS
        effective_max = (
            max(1000, min(max_output_chars, MAX_OUTPUT_CHARS))
            if max_output_chars is not None
            else DEFAULT_MAX_OUTPUT_CHARS
        )

        try:
            session_id, poll = await self._session_manager.start(
                command=command,
                cwd=cwd,
                env=env,
                timeout=timeout,
                yield_time_ms=effective_yield,
                max_output_chars=effective_max,
            )
        except RuntimeError as exc:
            return ToolResult.error(f"Error: {exc}")
        except Exception as exc:
            return ToolResult.error(f"Error starting exec session: {exc}")

        return _format_session_poll(session_id, poll, started=True)

    # ------------------------------------------------------------------
    # 基础辅助方法（step69 沿用）
    # ------------------------------------------------------------------

    def _resolve_cwd(self, working_dir: str | None) -> str:
        """解析工作目录。"""
        if working_dir:
            return working_dir
        if self.working_dir:
            return self.working_dir
        return os.getcwd()

    def _check_workspace_boundary(self, cwd: str) -> str | None:
        """检查工作目录是否在 workspace 内。"""
        access = current_tool_workspace(
            self.working_dir,
            restrict_to_workspace=True,
        )
        workspace_root = (
            str(access.project_path)
            if access.project_path is not None
            else self.working_dir
        )
        if not workspace_root:
            return None

        try:
            requested = Path(cwd).expanduser().resolve()
            resolved_root = Path(workspace_root).expanduser().resolve()
        except Exception:
            return "Error: working_dir could not be resolved."

        if not is_path_within(requested, resolved_root):
            return (
                "Error: working_dir is outside the configured workspace. "
                "This is a hard policy boundary, not a transient failure."
            )
        return None

    def _resolve_timeout(self, timeout: int | None) -> int | None:
        """解析有效超时。"""
        if timeout is not None:
            if timeout == 0:
                return None
            return min(timeout, self._MAX_TIMEOUT)
        if self.timeout and self.timeout > 0:
            return self.timeout
        return None

    def _truncate_output(self, text: str, max_len: int) -> str:
        """输出截断：头尾各保留一半。"""
        if len(text) <= max_len:
            return text
        half = max_len // 2
        truncated = len(text) - max_len
        return (
            text[:half]
            + f"\n\n... ({truncated:,} chars truncated) ...\n\n"
            + text[-half:]
        )

    @staticmethod
    async def _kill_process(process: asyncio.subprocess.Process) -> None:
        """杀死子进程并等待回收。"""
        if process.returncode is None:
            process.kill()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except (asyncio.TimeoutError, Exception):
                pass
