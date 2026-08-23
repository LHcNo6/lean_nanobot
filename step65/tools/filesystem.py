"""文件系统工具：写入文件（step65）。

对齐 nanobot `agent/tools/filesystem.py` 的最小子集：
- ``_FsTool``：文件工具共享基类（路径解析 + 文件状态追踪）；
- ``WriteFileTool``：写入文件工具（创建新文件或覆盖已有文件）。

后续 step 将在此文件中追加 ReadFileTool（迁移）、EditFileTool、ListDirTool。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from step65.config.schema import Base, FileToolsConfig
from step65.schema import StringSchema, tool_parameters_schema
from step65.security.workspace_access import current_tool_workspace
from step65.security.workspace_policy import WorkspaceBoundaryError, resolve_allowed_path
from step65.tool import Tool, ToolResult, tool_parameters
from step65.tools.file_state import FileStates, current_file_states


class _FsTool(Tool):
    """文件系统工具共享基类：路径解析 + 文件状态追踪。

    封装所有文件工具共有的逻辑：
    - 从 ``ToolContext`` 创建实例（``create`` 类方法）；
    - workspace 边界守卫下的路径解析（``_resolve_write``）；
    - 会话级文件状态追踪（``_file_states`` 属性）。

    子类应实现 ``name``/``description``/``execute``，并通过 ``@tool_parameters``
    声明参数 schema。
    """

    config_key = "file"

    @classmethod
    def config_cls(cls) -> type[Base]:
        """返回文件工具的配置类（对齐 nanobot ``Tool.config_cls`` 机制）。"""
        return FileToolsConfig

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        """根据配置判断文件工具是否启用。

        Args:
            ctx: ToolContext 实例，需包含 ``config.tools.file.enable``。

        Returns:
            True 表示启用，False 表示 ToolLoader 应跳过此工具。
        """
        return ctx.config.tools.file.enable

    def __init__(
        self,
        workspace: str = "",
        restrict_to_workspace: bool = False,
        file_states: FileStates | None = None,
        allowed_dir: str | None = None,
    ) -> None:
        """初始化文件工具基类。

        Args:
            workspace: 项目根目录（绝对路径字符串）。
            restrict_to_workspace: 是否限制文件访问在 workspace 内。
            file_states: 显式传入的 FileStates 实例（用于 dream/subagent
                等需要隔离状态的场景）；None 时运行时从 ContextVar 解析。
            allowed_dir: 显式允许根目录（受限模式下使用）。
        """
        self._workspace = workspace
        self._restrict = restrict_to_workspace
        self._explicit_file_states = file_states
        self._fallback_file_states = FileStates()
        self._allowed_dir = allowed_dir

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        """从 ToolContext 创建文件工具实例。

        从 ctx 中提取 workspace、权限意图和 file_state_store，按 session_key
        获取对应的 FileStates 实例，使 read-dedup 和 read-before-edit 限定
        在单个 agent 会话内。

        Args:
            ctx: ToolContext 实例。

        Returns:
            初始化后的工具实例。
        """
        restrict = ctx.config.tools.restrict_to_workspace
        allowed_dir = ctx.workspace if restrict else None
        file_states: FileStates | None = None
        if ctx.file_state_store is not None:
            file_states = ctx.file_state_store.for_session(ctx.session_key)
        return cls(
            workspace=ctx.workspace,
            restrict_to_workspace=restrict,
            file_states=file_states,
            allowed_dir=allowed_dir,
        )

    @property
    def _file_states(self) -> FileStates:
        """获取当前生效的 FileStates 实例。

        优先级：显式传入 > ContextVar 绑定 > 内置 fallback。
        与 nanobot ``_FsTool._file_states`` 语义一致。
        """
        if self._explicit_file_states is not None:
            return self._explicit_file_states
        return current_file_states(self._fallback_file_states)

    def _resolve_write(self, path: str) -> Path:
        """解析写入路径，应用 workspace 边界守卫。

        写操作不享受读操作的豁免目录（如内置技能目录），路径必须落在
        workspace 允许根内。

        Args:
            path: 用户传入的文件路径（绝对或 workspace 相对）。

        Returns:
            解析后的绝对 Path。

        Raises:
            WorkspaceBoundaryError: 路径越界时抛出。
        """
        access = current_tool_workspace(
            self._workspace,
            restrict_to_workspace=self._restrict,
        )
        return resolve_allowed_path(
            path,
            workspace=access.project_path or (self._workspace or None),
            allowed_root=access.allowed_root,
        )


# ---------------------------------------------------------------------------
# write_file
# ---------------------------------------------------------------------------


@tool_parameters(
    tool_parameters_schema(
        path=StringSchema("The file path to write to"),
        content=StringSchema("The content to write"),
        required=["path", "content"],
    )
)
class WriteFileTool(_FsTool):
    """写入文件内容：创建新文件或覆盖已有文件。

    功能：
    - 写入 UTF-8 文本内容；
    - 自动创建不存在的父目录；
    - 覆盖已有文件（无确认提示，agent 应自行判断）；
    - 写入后更新 FileStates，标记文件为"已写入"（不可 dedup）。

    对齐 nanobot ``filesystem.WriteFileTool``，简化了异常处理（不区分
    设备文件黑名单，Windows 下不适用）。
    """

    _scopes = {"core", "subagent", "memory"}

    @property
    def name(self) -> str:
        """工具名：``write_file``。"""
        return "write_file"

    @property
    def description(self) -> str:
        """工具描述：说明创建/覆盖文件的行为。"""
        return (
            "Create a new file or intentionally replace an entire file with "
            "the provided content. Overwrites existing files and creates parent "
            "directories as needed. For code changes or partial edits, prefer "
            "apply_patch; use edit_file only for small exact replacements."
        )

    @property
    def read_only(self) -> bool:
        """写操作有副作用，非只读。"""
        return False

    async def execute(
        self,
        path: str = "",
        content: str = "",
        **kwargs: Any,
    ) -> ToolResult:
        """执行文件写入。

        Args:
            path: 目标文件路径（绝对或 workspace 相对）。
            content: 要写入的文本内容。
            **kwargs: 忽略的额外参数（兼容 Tool.execute 签名）。

        Returns:
            成功时返回包含字符数和路径的 ToolResult；失败时返回
            ``ToolResult.error``。
        """
        if not path:
            return ToolResult.error("Error: write_file requires a 'path' parameter.")
        if content is None:
            return ToolResult.error("Error: write_file requires a 'content' parameter.")

        try:
            fp = self._resolve_write(path)
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(content, encoding="utf-8")
            self._file_states.record_write(fp)
            return ToolResult(f"Successfully wrote {len(content)} characters to {fp}")
        except WorkspaceBoundaryError as exc:
            return ToolResult.error(f"Error: {exc}")
        except PermissionError as exc:
            return ToolResult.error(f"Error: {exc}")
        except OSError as exc:
            return ToolResult.error(f"Error writing file: {exc}")
