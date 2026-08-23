"""CLI 应用白名单工具：CliAppsTool（step83）。

对齐 nanobot `agent/tools/cli_apps.py` 的最小子集：
- CliApp：CLI 应用元数据；
- CliAppManager：应用注册/查询/执行管理；
- CliAppsTool：执行已注册的 CLI 应用（argv 子进程，非 shell）。

简化版：内存存储，不实现应用安装/卸载、catalog 缓存、runtime context。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from step116.schema import ArraySchema, IntegerSchema, StringSchema, tool_parameters_schema
from step116.tool import Tool, ToolResult, tool_parameters


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class CliApp:
    """CLI 应用元数据。"""

    name: str
    command: str
    description: str = ""


# ---------------------------------------------------------------------------
# CliAppManager
# ---------------------------------------------------------------------------


class CliAppManager:
    """CLI 应用管理器：注册、查询、执行。"""

    def __init__(self) -> None:
        self._apps: dict[str, CliApp] = {}

    def register(self, app: CliApp) -> None:
        """注册一个 CLI 应用。同名应用会被覆盖。

        Args:
            app: CLI 应用元数据。
        """
        self._apps[app.name] = app

    def get(self, name: str) -> CliApp | None:
        """按名称获取应用。

        Args:
            name: 应用名称。

        Returns:
            CliApp 或 None。
        """
        return self._apps.get(name)

    def list_names(self) -> list[str]:
        """列出所有已注册应用名称。

        Returns:
            应用名称列表（排序）。
        """
        return sorted(self._apps.keys())

    def has(self, name: str) -> bool:
        """检查应用是否已注册。"""
        return name in self._apps

    async def run(
        self,
        name: str,
        args: list[str] | None = None,
        cwd: str | None = None,
        timeout: int = 60,
    ) -> str:
        """执行已注册的 CLI 应用。

        使用 argv 子进程（create_subprocess_exec），而非 shell。

        Args:
            name: 应用名称。
            args: 命令行参数列表。
            cwd: 工作目录。
            timeout: 超时秒数。

        Returns:
            stdout + stderr 文本。

        Raises:
            ValueError: 未知应用名称。
            asyncio.TimeoutError: 执行超时。
        """
        app = self.get(name)
        if app is None:
            available = ", ".join(self.list_names()) or "(none)"
            raise ValueError(f"Unknown CLI app '{name}'. Available: {available}")

        argv = [app.command] + (args or [])

        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise

        output = stdout.decode("utf-8", errors="replace")
        if stderr:
            err_text = stderr.decode("utf-8", errors="replace")
            if output:
                output += "\n" + err_text
            else:
                output = err_text

        return output


# ---------------------------------------------------------------------------
# 配置 -> 管理器
# ---------------------------------------------------------------------------


def build_cli_app_manager(cfg: Any | None) -> "CliAppManager":
    """从 cli_apps 配置构建 CliAppManager（step116）。

    Args:
        cfg: ``CliAppsConfig`` / duck-typed 对象 / ``None``。

    Returns:
        已注册应用的 ``CliAppManager``；``cfg`` 为 ``None`` 时返回空管理器。

    说明：
        - 允许 ``cfg`` 为任意提供 ``.apps``（list，每项含 ``name``/``command``/
          ``description``）的对象，便于测试与生产配置共用同一逻辑；
        - 主代理与子代理各自按同一配置调用本函数，得到内容等价的注册表
          （nanobot 为共享单例，本实现取等价做法）。
    """
    mgr = CliAppManager()
    if cfg is None:
        return mgr
    for spec in getattr(cfg, "apps", None) or []:
        mgr.register(CliApp(
            name=spec.name,
            command=spec.command,
            description=getattr(spec, "description", "") or "",
        ))
    return mgr


# ---------------------------------------------------------------------------
# CliAppsTool
# ---------------------------------------------------------------------------


@tool_parameters(
    tool_parameters_schema(
        name=StringSchema(
            "Installed CLI app registry name. Only apps explicitly registered are allowed."
        ),
        args=ArraySchema(
            StringSchema("One command-line argument."),
            description="Arguments to pass to the CLI entry point. Do not include the entry point itself.",
            nullable=True,
        ),
        working_dir=StringSchema("Optional working directory for the CLI call.", nullable=True),
        timeout=IntegerSchema(
            "Timeout in seconds for this CLI call.",
            minimum=1,
            maximum=600,
            nullable=True,
        ),
        required=["name"],
    )
)
class CliAppsTool(Tool):
    """执行已注册的 CLI 应用（argv 子进程，非 shell）。

    与 ExecTool 的区别：
    - ExecTool：shell 执行，任意命令；
    - CliAppsTool：argv 执行，仅限已注册应用。
    """

    _scopes = {"core", "subagent"}
    config_key = "cli_apps"

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        """是否启用：读取 config.cli_apps.enable（默认 True）。"""
        cfg = getattr(getattr(ctx, "config", None), "cli_apps", None)
        return getattr(cfg, "enable", True)

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        """从上下文创建工具实例。"""
        manager = getattr(ctx, "cli_app_manager", None)
        if manager is None:
            manager = CliAppManager()
        return cls(manager=manager, workspace=getattr(ctx, "workspace", None))

    def __init__(self, manager: CliAppManager, workspace: str | None = None):
        """初始化 CliAppsTool。

        Args:
            manager: CLI 应用管理器。
            workspace: 默认工作目录。
        """
        self._manager = manager
        self._workspace = workspace

    @property
    def name(self) -> str:
        """工具名：``run_cli_app``。"""
        return "run_cli_app"

    @property
    def description(self) -> str:
        """工具描述。"""
        try:
            installed = self._manager.list_names()
        except Exception:
            installed = []
        installed_note = (
            f" Installed CLI Apps: {', '.join(installed)}."
            if installed
            else " No CLI Apps are currently registered."
        )
        return (
            "Run a CLI App that is explicitly registered. "
            "Do not use this for ordinary system CLIs such as git, python, or npm; "
            "unknown names are rejected. Execution uses argv, not shell."
            + installed_note
        )

    @property
    def read_only(self) -> bool:
        """不是只读（可以执行应用）。"""
        return False

    async def execute(
        self,
        name: str | None = None,
        args: list[str] | None = None,
        working_dir: str | None = None,
        timeout: int | None = None,
        **kwargs: Any,
    ) -> str | ToolResult:
        """执行 CLI 应用。

        Args:
            name: 应用名称。
            args: 命令行参数。
            working_dir: 工作目录。
            timeout: 超时秒数。
            **kwargs: 忽略的额外参数。

        Returns:
            执行输出文本或错误。
        """
        if not name:
            return ToolResult.error("Error: 'name' is required.")

        effective_cwd = working_dir or self._workspace
        effective_timeout = timeout or 60

        try:
            output = await self._manager.run(
                name=name,
                args=args,
                cwd=effective_cwd,
                timeout=effective_timeout,
            )
            return output
        except ValueError as exc:
            return ToolResult.error(f"Error: {exc}")
        except asyncio.TimeoutError:
            return ToolResult.error(
                f"Error: CLI app '{name}' timed out after {effective_timeout} seconds"
            )
        except FileNotFoundError as exc:
            return ToolResult.error(f"Error: CLI app command not found: {exc}")
        except Exception as exc:
            return ToolResult.error(f"Error running CLI app '{name}': {exc}")
