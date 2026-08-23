"""Shell 执行工具：ExecTool 基础版（step69）。

对齐 nanobot `agent/tools/shell.py` 的最小子集：
- 同步 shell 命令执行（asyncio.create_subprocess_shell）；
- 超时控制（默认 60s，最大 600s）；
- 输出截断（默认 10000 字符，头尾保留）；
- 危险命令黑名单（rm -rf、format、mkfs、dd、shutdown 等）；
- workspace 边界检查（restrict_to_workspace 时 working_dir 不能越界）。

简化了 nanobot 的高级特性（交互式会话、环境变量管理、allow/deny 灵活过滤、
沙箱包装、shell 选择、login shell、path_prepend/append、流式输出）。
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path
from typing import Any

from step69.schema import IntegerSchema, StringSchema, tool_parameters_schema
from step69.security.workspace_access import current_tool_workspace
from step69.security.workspace_policy import is_path_within
from step69.tool import Tool, ToolResult, tool_parameters

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

_IS_WINDOWS = sys.platform == "win32"

# 危险命令黑名单：匹配到任意一个则拦截，不执行。
# 这是基础安全网，防止 agent 误执行破坏性命令。
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
        required=["command"],
    )
)
class ExecTool(Tool):
    """Shell 命令执行工具（基础版）。

    功能：
    - 执行 shell 命令并返回输出；
    - 超时控制，超时后杀死进程；
    - 输出截断，防止输出爆炸；
    - 危险命令黑名单，防止破坏性操作；
    - workspace 边界检查，受限模式下 working_dir 不能越界。

    对齐 nanobot ``shell.ExecTool``，简化了交互式会话、环境变量管理、
    灵活命令过滤、沙箱包装等高级特性。
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

        读取配置：
        - ``config.exec.timeout``：默认超时；
        - ``config.tools.restrict_to_workspace``：workspace 限制。
        """
        cfg = ctx.config.exec
        return cls(
            working_dir=ctx.workspace,
            timeout=getattr(cfg, "timeout", 60),
            restrict_to_workspace=getattr(ctx.config.tools, "restrict_to_workspace", False),
        )

    def __init__(
        self,
        timeout: int = 60,
        working_dir: str | None = None,
        restrict_to_workspace: bool = False,
        deny_patterns: list[str] | None = None,
    ):
        """初始化 ExecTool。

        Args:
            timeout: 默认超时秒数（0=不限制）。
            working_dir: 默认工作目录。
            restrict_to_workspace: 是否限制在 workspace 内。
            deny_patterns: 危险命令正则列表（None 时使用默认黑名单）。
        """
        self.timeout = timeout
        self.working_dir = working_dir
        self.restrict_to_workspace = restrict_to_workspace
        self.deny_patterns = deny_patterns if deny_patterns is not None else list(_DEFAULT_DENY_PATTERNS)

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
        """exec 不是只读操作（可以修改文件系统）。"""
        return False

    # ------------------------------------------------------------------
    # 主执行方法
    # ------------------------------------------------------------------

    async def execute(
        self,
        command: str | None = None,
        working_dir: str | None = None,
        timeout: int | None = None,
        **kwargs: Any,
    ) -> str | ToolResult:
        """执行 shell 命令。

        Args:
            command: 要执行的 shell 命令（必填）。
            working_dir: 命令执行的工作目录（默认 workspace 根）。
            timeout: 超时秒数（0=不限制，默认取配置值）。
            **kwargs: 忽略的额外参数。

        Returns:
            成功时返回命令输出文本（含退出码）；失败时返回 ``ToolResult.error``。
        """
        # 1. 参数校验
        if not command:
            return ToolResult.error("Error: Missing command.")

        # 2. 危险命令检查
        danger = self._check_dangerous(command)
        if danger:
            return ToolResult.error(danger)

        # 3. 解析工作目录
        cwd = self._resolve_cwd(working_dir)

        # 4. workspace 边界检查
        if self.restrict_to_workspace:
            boundary_error = self._check_workspace_boundary(cwd)
            if boundary_error:
                return ToolResult.error(boundary_error)

        # 5. 解析超时
        effective_timeout = self._resolve_timeout(timeout)

        # 6-10. 创建子进程、等待、组装输出、截断、返回
        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_shell(
                command,
                cwd=cwd,
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

            # 7. 组装输出
            output_parts: list[str] = []

            if stdout:
                output_parts.append(stdout.decode("utf-8", errors="replace"))

            if stderr:
                stderr_text = stderr.decode("utf-8", errors="replace")
                if stderr_text.strip():
                    output_parts.append(f"STDERR:\n{stderr_text}")

            exit_code = process.returncode if process.returncode is not None else -1
            output_parts.append(f"\nExit code: {exit_code}")

            result = "\n".join(output_parts) if output_parts else "(no output)"

            # 8. 输出截断
            result = self._truncate_output(result, self._MAX_OUTPUT)

            return result

        except Exception as exc:
            if process is not None:
                await self._kill_process(process)
            return ToolResult.error(f"Error executing command: {exc}")

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    def _check_dangerous(self, command: str) -> str | None:
        """危险命令检查。

        使用 ``re.search`` 检测命令是否匹配黑名单中的任意正则。

        Args:
            command: 要检查的命令字符串。

        Returns:
            被拦截时返回错误消息，安全时返回 None。
        """
        for pattern in self.deny_patterns:
            if re.search(pattern, command):
                return f"Error: Command blocked for safety: matches '{pattern}'."
        return None

    def _resolve_cwd(self, working_dir: str | None) -> str:
        """解析工作目录。

        优先级：调用参数 > 实例默认 > 当前进程目录。

        Args:
            working_dir: 调用参数中的工作目录。

        Returns:
            解析后的工作目录路径字符串。
        """
        if working_dir:
            return working_dir
        if self.working_dir:
            return self.working_dir
        return os.getcwd()

    def _check_workspace_boundary(self, cwd: str) -> str | None:
        """检查工作目录是否在 workspace 内。

        使用 ``current_tool_workspace`` 获取当前 workspace scope，
        然后用 ``is_path_within`` 检查 cwd 是否在 workspace 内。

        Args:
            cwd: 要检查的工作目录。

        Returns:
            越界时返回错误消息，合法时返回 None。
        """
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
        """解析有效超时。

        优先级：调用参数（不超过 _MAX_TIMEOUT）> 实例默认。
        0 表示不限制（返回 None）。

        Args:
            timeout: 调用参数中的超时。

        Returns:
            有效超时秒数，None 表示不限制。
        """
        if timeout is not None:
            if timeout == 0:
                return None
            return min(timeout, self._MAX_TIMEOUT)
        if self.timeout and self.timeout > 0:
            return self.timeout
        return None

    def _truncate_output(self, text: str, max_len: int) -> str:
        """输出截断：头尾各保留一半，中间省略。

        Args:
            text: 原始输出文本。
            max_len: 最大字符数。

        Returns:
            截断后的文本。
        """
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
        """杀死子进程并等待回收，避免僵尸进程。

        Args:
            process: 要杀死的子进程。
        """
        if process.returncode is None:
            process.kill()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except (asyncio.TimeoutError, Exception):
                pass
