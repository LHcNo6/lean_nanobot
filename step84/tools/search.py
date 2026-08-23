"""搜索工具：文件查找 + 内容搜索（step68）。

对齐 nanobot `agent/tools/search.py` 的最小子集：
- ``_SearchTool``：搜索工具共享基类（文件遍历 + 路径显示）；
- ``FindFilesTool``：按路径片段/glob/类型查找文件；
- ``GrepTool``：正则/纯文本内容搜索。

简化了 nanobot 的高级特性（include_dirs、sort=modified、offset 分页、
count 输出模式、context 上下文、legacy 别名等）。
"""

from __future__ import annotations

import fnmatch
import os
import re
from contextlib import suppress
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from step84.schema import BooleanSchema, IntegerSchema, StringSchema, tool_parameters_schema
from step84.tool import ToolResult, tool_parameters
from step84.tools.file_state import FileStates  # noqa: F401  (确保 _FsTool 依赖可导入)
from step84.tools.filesystem import ListDirTool, _FsTool

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

_DEFAULT_FILE_HEAD_LIMIT = 200
_DEFAULT_GREP_HEAD_LIMIT = 250
_MAX_FILE_BYTES = 2_000_000  # 跳过 >2MB 的文件

_TYPE_GLOB_MAP: dict[str, tuple[str, ...]] = {
    "py": ("*.py", "*.pyi"),
    "python": ("*.py", "*.pyi"),
    "js": ("*.js", "*.jsx", "*.mjs", "*.cjs"),
    "ts": ("*.ts", "*.tsx", "*.mts", "*.cts"),
    "tsx": ("*.tsx",),
    "jsx": ("*.jsx",),
    "json": ("*.json",),
    "md": ("*.md", "*.mdx"),
    "markdown": ("*.md", "*.mdx"),
    "yaml": ("*.yaml", "*.yml"),
    "yml": ("*.yaml", "*.yml"),
    "toml": ("*.toml",),
    "html": ("*.html", "*.htm"),
    "css": ("*.css", "*.scss", "*.sass"),
    "sh": ("*.sh", "*.bash"),
    "go": ("*.go",),
    "rs": ("*.rs",),
    "rust": ("*.rs",),
    "java": ("*.java",),
    "sql": ("*.sql",),
}


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _is_binary(raw: bytes) -> bool:
    """检测字节内容是否为二进制文件。

    规则：
    - 含 null 字节 → 二进制；
    - 前 4096 字节中非文本控制字符比例 > 20% → 二进制。

    Args:
        raw: 文件原始字节。

    Returns:
        True 表示二进制文件。
    """
    if b"\x00" in raw:
        return True
    sample = raw[:4096]
    if not sample:
        return False
    non_text = sum(byte < 9 or 13 < byte < 32 for byte in sample)
    return (non_text / len(sample)) > 0.2


def _match_glob(rel_path: str, name: str, pattern: str) -> bool:
    """glob 模式匹配。

    - 模式含 ``/`` 或以 ``**`` 开头 → 匹配完整相对路径；
    - 否则 → 仅匹配文件名。

    Args:
        rel_path: 相对于搜索根的路径（正斜杠）。
        name: 文件名。
        pattern: glob 模式。

    Returns:
        True 表示匹配。
    """
    normalized = pattern.strip().replace("\\", "/")
    if not normalized:
        return False
    if "/" in normalized or normalized.startswith("**"):
        return PurePosixPath(rel_path).match(normalized)
    return fnmatch.fnmatch(name, normalized)


def _matches_type(name: str, file_type: str | None) -> bool:
    """文件类型简写匹配。

    Args:
        name: 文件名。
        file_type: 类型简写（如 "py"、"md"），None 表示不限制。

    Returns:
        True 表示类型匹配。
    """
    if not file_type:
        return True
    lowered = file_type.strip().lower()
    if not lowered:
        return True
    patterns = _TYPE_GLOB_MAP.get(lowered, (f"*.{lowered}",))
    return any(fnmatch.fnmatch(name.lower(), p.lower()) for p in patterns)


def _matches_query(display_path: str, query: str | None) -> bool:
    """路径片段搜索：空白分隔的所有词都必须出现在路径中（不区分大小写）。

    Args:
        display_path: 显示用路径。
        query: 搜索词，空白分隔多个词。

    Returns:
        True 表示所有词都匹配。
    """
    if not query:
        return True
    haystack = display_path.lower()
    terms = [part for part in query.lower().split() if part]
    return all(term in haystack for term in terms)


# ---------------------------------------------------------------------------
# _SearchTool 基类
# ---------------------------------------------------------------------------


class _SearchTool(_FsTool):
    """搜索工具共享基类：文件遍历 + 路径显示。

    继承 ``_FsTool`` 获得路径解析和 workspace 边界守卫。提供：
    - ``_iter_files(root)``：递归遍历文件，自动跳过噪声目录；
    - ``_display_path(target, root)``：返回 workspace-relative 显示路径。
    """

    _IGNORE_DIRS = set(ListDirTool._IGNORE_DIRS)  # 复用 ListDirTool 的噪声目录列表

    def _display_path(self, target: Path, root: Path) -> str:
        """返回 workspace-relative 显示路径（正斜杠）。

        优先相对于 workspace 根，否则相对于搜索根。

        Args:
            target: 目标文件 Path。
            root: 搜索根目录 Path。

        Returns:
            正斜杠分隔的相对路径字符串。
        """
        workspace = self._display_workspace()
        if workspace:
            with suppress(ValueError):
                return target.relative_to(workspace).as_posix()
        return target.relative_to(root).as_posix()

    def _iter_files(self, root: Path) -> Iterable[Path]:
        """递归遍历文件，自动跳过噪声目录。

        使用 ``os.walk`` 并在遍历中修改 ``dirnames``，高效跳过噪声目录
        （不会进入 .git 等目录递归）。

        Args:
            root: 搜索根目录（或文件，文件时直接 yield）。

        Yields:
            每个文件的 Path。
        """
        if root.is_file():
            yield root
            return

        for dirpath, dirnames, filenames in os.walk(root):
            # 原地修改 dirnames，跳过噪声目录（os.walk 不会进入被移除的目录）
            dirnames[:] = sorted(d for d in dirnames if d not in self._IGNORE_DIRS)
            current = Path(dirpath)
            for filename in sorted(filenames):
                yield current / filename


# ---------------------------------------------------------------------------
# find_files
# ---------------------------------------------------------------------------


@tool_parameters(
    tool_parameters_schema(
        path=StringSchema("Directory or file to search in (default '.')"),
        query=StringSchema(
            "Case-insensitive path fragment search. "
            "Whitespace-separated terms must all be present."
        ),
        glob=StringSchema("File filter, e.g. '*.py' or 'tests/**/test_*.py'"),
        type=StringSchema("File type shorthand, e.g. 'py', 'ts', 'md', 'json'"),
        head_limit=IntegerSchema(
            "Maximum paths to return (default 200, 0 for all)",
            minimum=0,
        ),
        required=[],
    )
)
class FindFilesTool(_SearchTool):
    """文件查找工具：按路径片段/glob/类型查找文件。

    功能：
    - 递归遍历项目，自动过滤噪声目录；
    - 支持路径片段搜索（query）、glob 模式过滤、文件类型简写；
    - 返回 workspace-relative 路径列表；
    - 支持 head_limit 截断。

    对齐 nanobot ``search.FindFilesTool``，简化了 include_dirs、sort=modified、
    offset 分页等高级特性。
    """

    _scopes = {"core", "subagent"}

    @property
    def name(self) -> str:
        """工具名：``find_files``。"""
        return "find_files"

    @property
    def description(self) -> str:
        """工具描述。"""
        return (
            "Find files by path fragment, glob, or file type. "
            "Use this before read_file when you need to locate files. "
            "Returns workspace-relative paths and skips common dependency/build "
            "directories."
        )

    @property
    def read_only(self) -> bool:
        """文件查找是只读操作。"""
        return True

    async def execute(
        self,
        path: str = ".",
        query: str | None = None,
        glob: str | None = None,
        type: str | None = None,
        head_limit: int | None = None,
        **kwargs: Any,
    ) -> str | ToolResult:
        """执行文件查找。

        Args:
            path: 搜索根目录或文件（默认 "."）。
            query: 路径片段搜索词（空白分隔，不区分大小写）。
            glob: glob 过滤模式。
            type: 文件类型简写。
            head_limit: 最大返回数（0=不限制，默认 200）。
            **kwargs: 忽略的额外参数。

        Returns:
            成功时返回每行一个文件路径的文本；无匹配返回 "No files found"；
            失败时返回 ``ToolResult.error``。
        """
        try:
            target = self._resolve(path or ".")
            if not target.exists():
                return ToolResult.error(f"Error: Path not found: {path}")

            root = target if target.is_dir() else target.parent
            limit = (
                _DEFAULT_FILE_HEAD_LIMIT
                if head_limit is None
                else None if head_limit == 0 else head_limit
            )

            matches: list[str] = []
            for candidate in self._iter_files(target):
                rel_path = candidate.relative_to(root).as_posix()
                display_path = self._display_path(candidate, root)
                name = candidate.name

                if glob and not _match_glob(rel_path, name, glob):
                    continue
                if not _matches_type(name, type):
                    continue
                if not _matches_query(display_path, query):
                    continue
                matches.append(display_path)

            matches.sort()

            if not matches:
                return "No files found"

            total = len(matches)
            if limit is not None and total > limit:
                result = "\n".join(matches[:limit])
                result += f"\n\n(truncated, showing first {limit} of {total} files)"
                return result
            return "\n".join(matches)

        except PermissionError as exc:
            return ToolResult.error(f"Error: {exc}")
        except OSError as exc:
            return ToolResult.error(f"Error finding files: {exc}")


# ---------------------------------------------------------------------------
# grep
# ---------------------------------------------------------------------------


@tool_parameters(
    tool_parameters_schema(
        pattern=StringSchema("Regex or plain text pattern to search for"),
        path=StringSchema("File or directory to search in (default '.')"),
        glob=StringSchema("File filter, e.g. '*.py'"),
        type=StringSchema("File type shorthand, e.g. 'py', 'md'"),
        case_insensitive=BooleanSchema(description="Case-insensitive search (default false)"),
        fixed_strings=BooleanSchema(description="Treat pattern as plain text (default false)"),
        output_mode=StringSchema(
            "'content' (matching lines) or 'files_with_matches' (default)",
        ),
        head_limit=IntegerSchema(
            "Maximum results to return (default 250, 0 for all)",
            minimum=0,
        ),
        required=["pattern"],
    )
)
class GrepTool(_SearchTool):
    """内容搜索工具：在文件中搜索正则或纯文本模式。

    功能：
    - 支持正则表达式和纯文本两种模式；
    - 支持不区分大小写；
    - 两种输出模式：content（匹配行+行号）、files_with_matches（仅文件路径）；
    - 自动跳过二进制文件和 >2MB 的大文件；
    - 支持 glob/type 过滤和 head_limit 截断。

    对齐 nanobot ``search.GrepTool``，简化了 count 输出模式、context 上下文、
    legacy 别名等高级特性。
    """

    _scopes = {"core", "subagent"}

    @property
    def name(self) -> str:
        """工具名：``grep``。"""
        return "grep"

    @property
    def description(self) -> str:
        """工具描述。"""
        return (
            "Search file contents with a regex pattern. "
            "Default output_mode is files_with_matches (file paths only); "
            "use content mode for matching lines. "
            "Skips binary and files >2 MB. Supports glob/type filtering."
        )

    @property
    def read_only(self) -> bool:
        """内容搜索是只读操作。"""
        return True

    async def execute(
        self,
        pattern: str = "",
        path: str = ".",
        glob: str | None = None,
        type: str | None = None,
        case_insensitive: bool = False,
        fixed_strings: bool = False,
        output_mode: str = "files_with_matches",
        head_limit: int | None = None,
        **kwargs: Any,
    ) -> str | ToolResult:
        """执行内容搜索。

        Args:
            pattern: 正则或纯文本搜索模式（必填）。
            path: 搜索根目录或文件（默认 "."）。
            glob: glob 过滤模式。
            type: 文件类型简写。
            case_insensitive: 是否不区分大小写。
            fixed_strings: 是否按纯文本处理（非正则）。
            output_mode: "content" 或 "files_with_matches"（默认）。
            head_limit: 最大返回数（0=不限制，默认 250）。
            **kwargs: 忽略的额外参数。

        Returns:
            成功时返回搜索结果文本；无匹配返回 "No matches found"；
            失败时返回 ``ToolResult.error``。
        """
        if not pattern:
            return ToolResult.error("Error: grep requires a 'pattern' parameter.")

        try:
            target = self._resolve(path or ".")
            if not target.exists():
                return ToolResult.error(f"Error: Path not found: {path}")

            # 编译正则
            flags = re.IGNORECASE if case_insensitive else 0
            try:
                needle = re.escape(pattern) if fixed_strings else pattern
                regex = re.compile(needle, flags)
            except re.error as exc:
                return ToolResult.error(f"Error: invalid regex pattern: {exc}")

            if output_mode not in ("content", "files_with_matches"):
                output_mode = "files_with_matches"

            limit = (
                _DEFAULT_GREP_HEAD_LIMIT
                if head_limit is None
                else None if head_limit == 0 else head_limit
            )

            root = target if target.is_dir() else target.parent
            results: list[str] = []
            matching_files: list[str] = []
            truncated = False

            for file_path in self._iter_files(target):
                rel_path = file_path.relative_to(root).as_posix()
                if glob and not _match_glob(rel_path, file_path.name, glob):
                    continue
                if not _matches_type(file_path.name, type):
                    continue

                # 大文件和二进制文件跳过
                try:
                    raw = file_path.read_bytes()
                except OSError:
                    continue
                if len(raw) > _MAX_FILE_BYTES:
                    continue
                if _is_binary(raw):
                    continue

                try:
                    content = raw.decode("utf-8")
                except UnicodeDecodeError:
                    continue

                display_path = self._display_path(file_path, root)
                lines = content.splitlines()
                file_has_match = False

                for line_no, line in enumerate(lines, start=1):
                    if regex.search(line):
                        file_has_match = True
                        if output_mode == "content":
                            if limit is not None and len(results) >= limit:
                                truncated = True
                                break
                            results.append(f"{display_path}:{line_no}| {line}")

                if file_has_match and display_path not in matching_files:
                    matching_files.append(display_path)

                if truncated:
                    break

            if output_mode == "files_with_matches":
                if not matching_files:
                    return "No matches found"
                total = len(matching_files)
                if limit is not None and total > limit:
                    result = "\n".join(matching_files[:limit])
                    result += f"\n\n(truncated, showing first {limit} of {total} files)"
                    return result
                return "\n".join(matching_files)
            else:
                if not results:
                    return "No matches found"
                if truncated:
                    results.append(f"\n(truncated at {limit} matches)")
                return "\n".join(results)

        except PermissionError as exc:
            return ToolResult.error(f"Error: {exc}")
        except OSError as exc:
            return ToolResult.error(f"Error searching: {exc}")
