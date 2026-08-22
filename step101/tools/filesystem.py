"""文件系统工具：写入文件 + 精确编辑（step65-66）。

对齐 nanobot `agent/tools/filesystem.py` 的最小子集：
- ``_FsTool``：文件工具共享基类（路径解析 + 文件状态追踪）；
- ``WriteFileTool``：写入文件工具（创建新文件或覆盖已有文件）；
- ``EditFileTool``：精确字符串替换工具（step66 新增）。

后续 step 将在此文件中追加 ReadFileTool（迁移）、ListDirTool。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from step101.config.schema import Base, FileToolsConfig
from step101.schema import BooleanSchema, IntegerSchema, StringSchema, tool_parameters_schema
from step101.security.workspace_access import current_tool_workspace
from step101.security.workspace_policy import WorkspaceBoundaryError, resolve_allowed_path
from step101.tool import Tool, ToolResult, tool_parameters
from step101.tools.file_state import FileStates, current_file_states


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

    def _resolve_read(self, path: str) -> Path:
        """解析读取路径，应用 workspace 边界守卫。

        读操作与写操作当前使用相同的边界策略（learn_nano 简化版未实现
        额外豁免目录）。未来如添加读豁免目录（如内置技能目录），在此方法
        中传入 ``extra_allowed_roots``。

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

    def _resolve(self, path: str) -> Path:
        """默认路径解析（读语义），对齐 nanobot ``_FsTool._resolve``。"""
        return self._resolve_read(path)

    def _display_workspace(self) -> Path | None:
        """返回当前 workspace 的 Path（用于显示相对路径）。

        对齐 nanobot ``_FsTool._display_workspace``。通过
        ``current_tool_workspace`` 获取当前 turn 的 workspace scope。
        """
        access = current_tool_workspace(self._workspace)
        return access.project_path

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


# ---------------------------------------------------------------------------
# edit_file（step66）
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _MatchSpan:
    """单个 old_text 匹配的位置信息。

    Attributes:
        start: 在 content 中的起始偏移（0-indexed）。
        end: 结束偏移（exclusive）。
        text: 匹配到的实际文本。
        line: 起始行号（1-indexed，用于错误提示）。
    """

    start: int
    end: int
    text: str
    line: int


def _find_matches(content: str, old_text: str) -> list[_MatchSpan]:
    """在 content 中查找所有 old_text 的精确匹配，返回位置列表。

    使用 ``str.find()`` 循环查找，每次找到后从 ``idx + max(1, len(old_text))``
    继续，避免空字符串匹配导致的无限循环。

    Args:
        content: 要搜索的文本内容。
        old_text: 要查找的精确文本。

    Returns:
        匹配位置列表，按出现顺序排列。空列表表示无匹配。
    """
    matches: list[_MatchSpan] = []
    if not old_text:
        return matches
    start = 0
    while True:
        idx = content.find(old_text, start)
        if idx == -1:
            break
        matches.append(
            _MatchSpan(
                start=idx,
                end=idx + len(old_text),
                text=content[idx : idx + len(old_text)],
                line=content.count("\n", 0, idx) + 1,
            )
        )
        start = idx + max(1, len(old_text))
    return matches


@tool_parameters(
    tool_parameters_schema(
        path=StringSchema("The file path to edit"),
        old_text=StringSchema("The exact text to replace (copy from read_file output)"),
        new_text=StringSchema("The replacement text"),
        replace_all=BooleanSchema(description="Replace all occurrences (default false)"),
        occurrence=IntegerSchema(
            "Replace the Nth occurrence (1-indexed); cannot use with replace_all",
            minimum=1,
        ),
        required=["path", "old_text", "new_text"],
    )
)
class EditFileTool(_FsTool):
    """精确字符串替换工具：在文件中用 new_text 替换 old_text。

    功能：
    - 精确匹配 old_text 并替换为 new_text；
    - 支持 ``replace_all=True`` 替换所有匹配；
    - 支持 ``occurrence=N`` 选择第 N 个匹配（1-indexed）；
    - 多匹配且无参数时返回歧义警告（不执行替换）；
    - 集成 ``FileStates.check_read`` 实现 read-before-edit 警告；
    - 保留原文件的 CRLF/LF 换行风格。

    对齐 nanobot ``filesystem.EditFileTool`` 的核心功能，简化了高级特性
    （line_hint、引号风格保留、缩进保留、最佳匹配诊断等留待后续增强）。
    """

    _scopes = {"core", "subagent", "memory"}

    @property
    def name(self) -> str:
        """工具名：``edit_file``。"""
        return "edit_file"

    @property
    def description(self) -> str:
        """工具描述：说明精确替换的行为和参数。"""
        return (
            "Perform a small, exact replacement in one file by replacing "
            "old_text with new_text. Use this for narrow text substitutions "
            "with old_text copied from read_file. If old_text matches multiple "
            "times, provide more context or set occurrence or replace_all=true."
        )

    @property
    def read_only(self) -> bool:
        """编辑操作有副作用，非只读。"""
        return False

    async def execute(
        self,
        path: str = "",
        old_text: str = "",
        new_text: str = "",
        replace_all: bool = False,
        occurrence: int | None = None,
        **kwargs: Any,
    ) -> ToolResult | str:
        """执行精确字符串替换。

        Args:
            path: 要编辑的文件路径（绝对或 workspace 相对）。
            old_text: 要替换的精确文本（从 read_file 输出复制）。
            new_text: 替换文本。
            replace_all: 是否替换所有匹配（默认 False）。
            occurrence: 替换第 N 个匹配（1-indexed），不能与 replace_all 同用。
            **kwargs: 忽略的额外参数（兼容 Tool.execute 签名）。

        Returns:
            成功时返回 ``ToolResult("Successfully edited {path}")``，如有
            read-before-edit 警告则前缀警告文本；多匹配歧义时返回普通字符串
            警告（不执行替换）；失败时返回 ``ToolResult.error``。
        """
        # --- 参数校验 ---
        if not path:
            return ToolResult.error("Error: edit_file requires a 'path' parameter.")
        if old_text is None:
            return ToolResult.error("Error: edit_file requires an 'old_text' parameter.")
        if new_text is None:
            return ToolResult.error("Error: edit_file requires a 'new_text' parameter.")
        if occurrence is not None and occurrence < 1:
            return ToolResult.error("Error: occurrence must be >= 1.")
        if replace_all and occurrence is not None:
            return ToolResult.error("Error: occurrence cannot be used with replace_all=true.")

        try:
            # --- 路径解析 ---
            fp = self._resolve_write(path)

            # --- 文件存在性检查 ---
            if not fp.exists():
                return ToolResult.error(f"Error: File not found: {path}")

            # --- 读取文件 ---
            raw = fp.read_bytes()
            uses_crlf = b"\r\n" in raw
            content = raw.decode("utf-8").replace("\r\n", "\n")

            # --- read-before-edit 检查 ---
            warning = self._file_states.check_read(fp)

            # --- 匹配查找 ---
            norm_old = old_text.replace("\r\n", "\n")
            matches = _find_matches(content, norm_old)

            if not matches:
                return ToolResult.error(
                    f"Error: old_text not found in {path}. Verify the file content."
                )

            count = len(matches)

            # --- 匹配选择 ---
            if replace_all:
                selected = matches
            elif occurrence is not None:
                if occurrence > count:
                    return ToolResult.error(
                        f"Error: occurrence {occurrence} is out of range; "
                        f"old_text appears {count} time(s)."
                    )
                selected = [matches[occurrence - 1]]
            elif count == 1:
                selected = [matches[0]]
            else:
                # 多匹配且无参数 → 返回歧义警告（不执行替换）
                line_numbers = [match.line for match in matches]
                preview = ", ".join(f"line {n}" for n in line_numbers[:3])
                if len(line_numbers) > 3:
                    preview += ", ..."
                return (
                    f"Warning: old_text appears {count} times at {preview}. "
                    "Provide more context, set occurrence to choose one match, "
                    "or set replace_all=true."
                )

            # --- 执行替换（倒序，避免位置偏移） ---
            norm_new = new_text.replace("\r\n", "\n")
            new_content = content
            for match in reversed(selected):
                new_content = new_content[: match.start] + norm_new + new_content[match.end :]

            # --- 写回文件 ---
            if uses_crlf:
                new_content = new_content.replace("\n", "\r\n")
            fp.write_bytes(new_content.encode("utf-8"))
            self._file_states.record_write(fp)

            # --- 返回结果 ---
            msg = f"Successfully edited {fp}"
            if warning:
                msg = f"{warning}\n{msg}"
            return ToolResult(msg)

        except WorkspaceBoundaryError as exc:
            return ToolResult.error(f"Error: {exc}")
        except PermissionError as exc:
            return ToolResult.error(f"Error: {exc}")
        except OSError as exc:
            return ToolResult.error(f"Error editing file: {exc}")


# ---------------------------------------------------------------------------
# list_dir（step67）
# ---------------------------------------------------------------------------


@tool_parameters(
    tool_parameters_schema(
        path=StringSchema("The directory path to list"),
        recursive=BooleanSchema(description="Recursively list all files (default false)"),
        max_entries=IntegerSchema(
            "Maximum entries to return (default 200)",
            minimum=1,
        ),
        required=["path"],
    )
)
class ListDirTool(_FsTool):
    """目录列表工具：列出目录内容，支持递归遍历。

    功能：
    - 非递归模式：列出目录的直接子项；
    - 递归模式：遍历所有子目录和文件；
    - 自动过滤噪声目录（.git、node_modules、__pycache__ 等）；
    - 结果超过 max_entries 时截断并提示；
    - 目录项带 ``/`` 后缀以区分文件。

    对齐 nanobot ``filesystem.ListDirTool``，简化了输出格式（不用 emoji，
    用 ``/`` 后缀区分目录）。
    """

    _scopes = {"core", "subagent"}

    _DEFAULT_MAX = 200
    _IGNORE_DIRS = frozenset({
        ".git", "node_modules", "__pycache__", ".venv", "venv",
        "dist", "build", ".tox", ".mypy_cache", ".pytest_cache",
        ".ruff_cache", ".coverage", "htmlcov",
    })

    @property
    def name(self) -> str:
        """工具名：``list_dir``。"""
        return "list_dir"

    @property
    def description(self) -> str:
        """工具描述：说明目录列表功能和参数。"""
        return (
            "List the contents of a directory. "
            "Set recursive=true to explore nested structure. "
            "Common noise directories (.git, node_modules, __pycache__, etc.) "
            "are auto-ignored."
        )

    @property
    def read_only(self) -> bool:
        """目录列表是只读操作，无副作用。"""
        return True

    async def execute(
        self,
        path: str = "",
        recursive: bool = False,
        max_entries: int | None = None,
        **kwargs: Any,
    ) -> str | ToolResult:
        """执行目录列表。

        Args:
            path: 要列出的目录路径（绝对或 workspace 相对）。
            recursive: 是否递归遍历子目录（默认 False）。
            max_entries: 最大返回条目数（默认 200）。
            **kwargs: 忽略的额外参数（兼容 Tool.execute 签名）。

        Returns:
            成功时返回纯文本目录列表（每行一个条目，目录带 ``/`` 后缀）；
            空目录返回空目录消息；失败时返回 ``ToolResult.error``。
        """
        if not path:
            return ToolResult.error("Error: list_dir requires a 'path' parameter.")

        try:
            dp = self._resolve(path)

            if not dp.exists():
                return ToolResult.error(f"Error: Directory not found: {path}")
            if not dp.is_dir():
                return ToolResult.error(f"Error: Not a directory: {path}")

            cap = max_entries or self._DEFAULT_MAX
            items: list[str] = []
            total = 0

            if recursive:
                # 递归遍历：rglob("*") 匹配所有子项
                for item in sorted(dp.rglob("*")):
                    # 路径中任何一段是噪声目录则跳过
                    if any(p in self._IGNORE_DIRS for p in item.parts):
                        continue
                    total += 1
                    if len(items) < cap:
                        rel = item.relative_to(dp)
                        items.append(f"{rel.as_posix()}/" if item.is_dir() else rel.as_posix())
            else:
                # 非递归：只列直接子项
                for item in sorted(dp.iterdir()):
                    if item.name in self._IGNORE_DIRS:
                        continue
                    total += 1
                    if len(items) < cap:
                        items.append(f"{item.name}/" if item.is_dir() else item.name)

            if total == 0:
                return f"Directory {path} is empty"

            result = "\n".join(items)
            if total > cap:
                result += f"\n\n(truncated, showing first {cap} of {total} entries)"
            return result

        except WorkspaceBoundaryError as exc:
            return ToolResult.error(f"Error: {exc}")
        except PermissionError as exc:
            return ToolResult.error(f"Error: {exc}")
        except OSError as exc:
            return ToolResult.error(f"Error listing directory: {exc}")


# ---------------------------------------------------------------------------
# ReadFileTool（step76：从 read_file.py 迁移 + 行号分页升级）
# ---------------------------------------------------------------------------


@tool_parameters(tool_parameters_schema(
    path=StringSchema("Path to the file to read (absolute, or relative to the workspace)"),
    offset=IntegerSchema("Starting line number (1-based, default 1)", minimum=1, maximum=1_000_000),
    limit=IntegerSchema("Maximum number of lines to return (default: no limit)", minimum=1, maximum=1_000_000, nullable=True),
    max_chars=IntegerSchema("Maximum characters to return (default 60000)", minimum=1, maximum=1_000_000),
    required=["path"],
))
class ReadFileTool(_FsTool):
    """读取文本文件，支持行号分页。

    step76 从 `tools/read_file.py` 迁移到 `tools/filesystem.py`，继承 `_FsTool`，
    新增 `offset`/`limit` 行号分页参数，输出格式为 `LINE_NUM|CONTENT`，
    与 edit_file/apply_patch 的行号引用一致。
    """

    _scopes = {"core", "subagent"}
    _MAX_CHARS = 128_000

    @property
    def name(self) -> str:
        """工具名：`read_file`。"""
        return "read_file"

    @property
    def description(self) -> str:
        """工具描述。"""
        return (
            "Read a text file from disk. Output format: LINE_NUM|CONTENT. "
            "Use offset and limit for large files. "
            "Reads exceeding max_chars are truncated."
        )

    @property
    def read_only(self) -> bool:
        """只读工具。"""
        return True

    async def execute(
        self,
        path: str = "",
        offset: int = 1,
        limit: int | None = None,
        max_chars: int = 60_000,
        **kwargs: Any,
    ) -> str | ToolResult:
        """读取文件内容，支持行号分页。

        Args:
            path: 文件路径。
            offset: 起始行号（1-based）。
            limit: 返回行数（None=不限制）。
            max_chars: 最大字符数。
            **kwargs: 忽略的额外参数。

        Returns:
            成功时返回格式化文本；失败时返回 `ToolResult.error`。
        """
        if not path:
            return ToolResult.error("Error: read_file requires a 'path' parameter.")

        try:
            fp = self._resolve_read(path)
        except WorkspaceBoundaryError as exc:
            return ToolResult.error(f"Error: {exc}")

        if not fp.exists():
            return ToolResult.error(f"Error: File not found: {path}")
        if not fp.is_file():
            return ToolResult.error(f"Error: Not a file: {path}")

        try:
            raw = fp.read_bytes()
        except PermissionError as exc:
            return ToolResult.error(f"Error: {exc}")
        except OSError as exc:
            return ToolResult.error(f"Error reading file: {exc}")

        if not raw:
            return f"(Empty file: {path})"

        # 检测编码（优先 UTF-8，失败时用 latin-1 兜底）
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("latin-1", errors="replace")

        # 按行分割（保留行内容，不保留换行符）
        lines = text.splitlines()
        total_lines = len(lines)

        # 应用 offset（1-based -> 0-based）
        start = max(0, offset - 1)
        if start >= total_lines:
            return f"(File has {total_lines} lines; offset {offset} is beyond end.)"

        # 应用 limit
        end = total_lines if limit is None else min(start + limit, total_lines)
        selected = lines[start:end]

        # 格式化为 LINE_NUM|CONTENT
        formatted = []
        char_count = 0
        truncated = False
        for i, line in enumerate(selected, start=start + 1):
            entry = f"{i}|{line}"
            if char_count + len(entry) + 1 > max_chars:
                truncated = True
                break
            formatted.append(entry)
            char_count += len(entry) + 1

        result = "\n".join(formatted)

        # 添加截断/分页信息
        info_parts = []
        if truncated:
            info_parts.append(f"truncated at {max_chars} chars")
        if end < total_lines or start > 0:
            info_parts.append(f"showing lines {start + 1}-{end} of {total_lines}")
        if info_parts:
            result += f"\n\n[{' | '.join(info_parts)}]"

        # 记录文件状态（读取追踪）
        try:
            states = current_file_states()
            if states is not None:
                states.record_read(fp, offset=offset, limit=limit)
        except Exception:
            pass  # 文件状态记录失败不影响读取

        return result
