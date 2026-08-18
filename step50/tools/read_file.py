from __future__ import annotations

from pathlib import Path
from typing import Any

from step50.schema import StringSchema, IntegerSchema, tool_parameters_schema
from step50.security.workspace_access import current_tool_workspace
from step50.security.workspace_policy import WorkspaceBoundaryError, resolve_allowed_path
from step50.skills.loader import BUILTIN_SKILLS_DIR
from step50.tool import Tool, ToolResult, tool_parameters


@tool_parameters(tool_parameters_schema(
    path=StringSchema("Path to the file to read (absolute, or relative to the workspace)"),
    max_chars=IntegerSchema(
        "Maximum characters to return (default 60000)",
        minimum=1, maximum=1_000_000,
    ),
    required=["path"],
))
class ReadFileTool(Tool):
    """读取文本文件内容。

    step29 演示工具：消费 Workspace 安全模型 ——
    - 工具在 turn 内通过 ``current_tool_workspace()`` 查询当前绑定的
      workspace scope；
    - ``restrict_to_workspace`` 开启时，路径必须落在允许根内（workspace 根 +
      内置技能目录豁免），否则抛 ``WorkspaceBoundaryError`` 并返回错误结果；
    - 未开启限制时仅按 workspace 解析相对路径（绝对路径直通）。

    注意：这是应用级守卫（对齐 nanobot 的边界注记语义），不替代 OS sandbox。
    """

    def __init__(self, workspace: str = "", restrict_to_workspace: bool = False) -> None:
        """初始化工具。

        Args:
            workspace: 默认 workspace（ContextVar 无绑定时回退用）。
            restrict_to_workspace: 构造期权限意图（ContextVar 无绑定时回退用）。
        """
        self._workspace = workspace
        self._restrict = restrict_to_workspace

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        # step29: 工具从 ToolContext 拿到真实 workspace 与权限意图
        # （装配时由 loop 解析的 WorkspaceScope 提供）。
        return cls(
            workspace=ctx.workspace if ctx is not None else "",
            restrict_to_workspace=(
                ctx.restrict_to_workspace if ctx is not None else False
            ),
        )

    @property
    def name(self) -> str:
        return "read_file"

    @property
    def description(self) -> str:
        return (
            "Reads a text file from disk (UTF-8). "
            "When workspace restriction is enabled, the path must stay inside "
            "the workspace; other files are rejected with a boundary error. "
            "Use it to read SKILL.md files referenced by the Skills section."
        )

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, path: str = "", max_chars: int = 60_000, **kwargs: Any) -> ToolResult:
        if not path:
            return ToolResult.error("Error: read_file requires a 'path' parameter.")

        access = current_tool_workspace(
            self._workspace,
            restrict_to_workspace=self._restrict,
        )
        try:
            resolved = resolve_allowed_path(
                path,
                workspace=access.project_path or (self._workspace or None),
                allowed_root=access.allowed_root,
                # 内置技能目录豁免：受限时仍允许读 SKILL.md（对齐 nanobot
                # filesystem 工具的 extra_read_allowed_dirs）。
                extra_allowed_roots=[BUILTIN_SKILLS_DIR] if access.allowed_root is not None else None,
            )
        except WorkspaceBoundaryError as exc:
            return ToolResult.error(f"Error: {exc}")
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            return ToolResult.error(f"Error: invalid path: {exc}")

        try:
            text = resolved.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return ToolResult.error(f"Error: File not found: {resolved}")
        except IsADirectoryError:
            return ToolResult.error(f"Error: Is a directory: {resolved}")
        except OSError as exc:
            return ToolResult.error(f"Error: Cannot read {resolved}: {exc}")

        if len(text) > max_chars:
            text = text[:max_chars] + "\n...[truncated]"
        return ToolResult(f"```\n{text}\n```")