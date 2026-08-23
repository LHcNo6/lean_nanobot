"""Glob 模式匹配工具（step77）。

对齐 nanobot `agent/tools/glob_tool.py` 的最小子集：
- 标准 glob 模式匹配（``*``, ``?``, ``**``, ``[seq]``）；
- 递归匹配（``**``）；
- 结果按相对路径输出（as_posix）；
- 最大结果数限制。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from step78.schema import IntegerSchema, StringSchema, tool_parameters_schema
from step78.security.workspace_policy import WorkspaceBoundaryError
from step78.tool import Tool, ToolResult, tool_parameters
from step78.tools.filesystem import _FsTool


@tool_parameters(tool_parameters_schema(
    pattern=StringSchema("Glob pattern to match files (e.g. '**/*.py', 'test_*.py')"),
    path=StringSchema("Directory to search in (default: workspace root)"),
    max_results=IntegerSchema("Maximum number of results to return (default 200)", minimum=1, maximum=10000),
    required=["pattern"],
))
class GlobTool(_FsTool):
    """Glob 模式匹配工具。

    支持标准 glob 语法：
    - ``*``：匹配任意字符（不含路径分隔符）；
    - ``?``：匹配单个字符；
    - ``**``：递归匹配任意层级目录；
    - ``[seq]``：匹配字符集合中的任意一个。
    """

    _scopes = {"core"}

    @property
    def name(self) -> str:
        """工具名：``glob``。"""
        return "glob"

    @property
    def description(self) -> str:
        """工具描述。"""
        return (
            "Find files matching a glob pattern. "
            "Supports *, ?, ** (recursive), and [seq] patterns. "
            "Returns relative paths from the search directory."
        )

    @property
    def read_only(self) -> bool:
        """只读工具。"""
        return True

    async def execute(
        self,
        pattern: str = "",
        path: str = ".",
        max_results: int = 200,
        **kwargs: Any,
    ) -> str | ToolResult:
        """执行 glob 匹配。

        Args:
            pattern: glob 模式。
            path: 搜索起始路径。
            max_results: 最大结果数。
            **kwargs: 忽略的额外参数。

        Returns:
            成功时返回文件列表文本；失败时返回 ``ToolResult.error``。
        """
        if not pattern:
            return ToolResult.error("Error: glob requires a 'pattern' parameter.")

        try:
            base = self._resolve_read(path)
        except WorkspaceBoundaryError as exc:
            return ToolResult.error(f"Error: {exc}")

        if not base.exists():
            return ToolResult.error(f"Error: Directory not found: {path}")
        if not base.is_dir():
            return ToolResult.error(f"Error: Not a directory: {path}")

        try:
            # pathlib.Path.glob 支持 ** 递归
            matches = list(base.glob(pattern))
        except Exception as exc:
            return ToolResult.error(f"Error: Invalid glob pattern '{pattern}': {exc}")

        # 只保留文件，排除目录
        files = [m for m in matches if m.is_file()]

        # 转换为相对路径（as_posix 跨平台）
        try:
            relative = [m.relative_to(base).as_posix() for m in files]
        except ValueError:
            relative = [m.as_posix() for m in files]

        # 排序
        relative.sort()

        # 限制结果数
        total = len(relative)
        shown = relative[:max_results]

        if total == 0:
            return f"No files found matching '{pattern}' in {path}"

        lines = [f"Found {total} file(s) matching '{pattern}':"]
        lines.extend(f"  {p}" for p in shown)

        if total > max_results:
            lines.append(f"\n(truncated, showing first {max_results} of {total})")

        return "\n".join(lines)
