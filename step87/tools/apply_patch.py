"""统一补丁应用工具：ApplyPatchTool（step74）。

对齐 nanobot `agent/tools/apply_patch.py` 的核心功能：
- 多文件批量编辑（单次调用最多 20 个 edit）；
- 两种操作：replace（精确替换）和 add（追加/创建）；
- dry_run 模式（验证+预览，不写入）；
- CRLF 换行符保留；
- 原子写入（备份+回滚）；
- diff 统计（+added/-deleted）。

继承 ``_FsTool``，复用路径解析和 workspace 边界守卫。
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from step87.schema import (
    ArraySchema,
    BooleanSchema,
    ObjectSchema,
    StringSchema,
    tool_parameters_schema,
)
from step87.tool import ToolResult, tool_parameters
from step87.tools.filesystem import _FsTool


# ---------------------------------------------------------------------------
# 数据类与异常
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _PatchSummary:
    """补丁操作摘要。"""

    action: str
    path: str
    added: int = 0
    deleted: int = 0


class _PatchError(ValueError):
    """补丁应用错误。"""


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _validate_patch_path(path: str) -> str:
    """校验补丁路径。

    Args:
        path: 原始路径字符串。

    Returns:
        清洗后的路径。

    Raises:
        _PatchError: 路径为空或含 null 字节。
    """
    normalized = path.strip()
    if not normalized:
        raise _PatchError("patch path cannot be empty")
    if "\0" in normalized:
        raise _PatchError(f"patch path contains a null byte: {path!r}")
    return normalized


def _append_text(content: str, addition: str) -> str:
    """追加文本，避免合并到未终止的最后一行。

    Args:
        content: 原内容。
        addition: 要追加的文本。

    Returns:
        追加后的内容（以换行符结尾）。
    """
    base = content.replace("\r\n", "\n")
    extra = addition.replace("\r\n", "\n")
    if base and extra and not base.endswith("\n") and not extra.startswith("\n"):
        base += "\n"
    combined = base + extra
    if combined and not combined.endswith("\n"):
        combined += "\n"
    return combined


def _line_diff_stats(before: str, after: str) -> tuple[int, int]:
    """计算行级 diff 统计（新增/删除行数）。

    Args:
        before: 修改前内容。
        after: 修改后内容。

    Returns:
        (新增行数, 删除行数)。
    """
    before_lines = before.replace("\r\n", "\n").splitlines()
    after_lines = after.replace("\r\n", "\n").splitlines()
    added = 0
    deleted = 0
    matcher = difflib.SequenceMatcher(a=before_lines, b=after_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag in ("replace", "delete"):
            deleted += i2 - i1
        if tag in ("replace", "insert"):
            added += j2 - j1
    return added, deleted


def _text_line_count(text: str) -> int:
    """计算文本行数。"""
    if not text:
        return 0
    return len(text.splitlines())


def _format_summary(summary: _PatchSummary) -> str:
    """格式化补丁摘要为一行文本。"""
    stats = ""
    if summary.added or summary.deleted:
        stats = f" (+{summary.added}/-{summary.deleted})"
    return f"- {summary.action} {summary.path}{stats}"


# ---------------------------------------------------------------------------
# ApplyPatchTool
# ---------------------------------------------------------------------------


@tool_parameters(
    tool_parameters_schema(
        edits=ArraySchema(
            items=ObjectSchema(
                path=StringSchema(
                    "Path to the file to edit. Relative paths resolve against the workspace."
                ),
                action=StringSchema(
                    "Operation type: replace or add.",
                    enum=["replace", "add"],
                ),
                old_text=StringSchema(
                    "Exact text to search for. Required for replace.",
                    nullable=True,
                ),
                new_text=StringSchema(
                    "Text to replace with or append. Required for replace and add.",
                    nullable=True,
                ),
                required=["path", "action"],
            ),
            description="List of edits to apply (1-20).",
            min_items=1,
            max_items=20,
        ),
        dry_run=BooleanSchema(
            description="Validate and summarize without writing files.",
            default=False,
        ),
        required=["edits"],
    )
)
class ApplyPatchTool(_FsTool):
    """统一补丁应用工具：多文件批量编辑。

    支持 replace（精确替换）和 add（追加/创建）两种操作，
    单次调用可编辑多个文件，支持 dry_run 预览。

    对齐 nanobot ``apply_patch.ApplyPatchTool``。
    """

    _scopes = {"core", "subagent"}

    @property
    def name(self) -> str:
        """工具名：``apply_patch``。"""
        return "apply_patch"

    @property
    def description(self) -> str:
        """工具描述。"""
        return (
            "Apply structured multi-file edits. Each edit specifies path, "
            "action (replace/add), and text. Use dry_run=true to preview. "
            "replace requires exact old_text; add appends or creates files."
        )

    @property
    def read_only(self) -> bool:
        """补丁应用不是只读操作。"""
        return False

    async def execute(
        self,
        edits: list[dict] | None = None,
        dry_run: bool = False,
        **kwargs: Any,
    ) -> str | ToolResult:
        """执行补丁应用。

        Args:
            edits: 编辑操作列表（1-20个）。
            dry_run: 是否只验证不写入。
            **kwargs: 忽略的额外参数。

        Returns:
            成功时返回格式化摘要；失败时返回 ``ToolResult.error``。
        """
        try:
            if not edits:
                raise _PatchError("must provide edits")

            writes: dict[Path, str] = {}
            summaries: list[_PatchSummary] = []

            for edit in edits:
                if not isinstance(edit, dict):
                    raise _PatchError("each edit must be an object")

                raw_path = edit.get("path")
                if not isinstance(raw_path, str):
                    raise _PatchError("path required for edit")
                path = _validate_patch_path(raw_path)

                action = edit.get("action")
                if not isinstance(action, str):
                    raise _PatchError(f"action required for edit: {path}")

                source = self._resolve_write(path)

                if action == "add":
                    self._apply_add(edit, source, path, writes, summaries)
                elif action == "replace":
                    self._apply_replace(edit, source, path, writes, summaries)
                else:
                    raise _PatchError(f"unknown action: {action}")

            if dry_run:
                return "Patch dry-run succeeded:\n" + "\n".join(
                    _format_summary(s) for s in summaries
                )

            # 原子写入：备份 → 写入 → 失败回滚
            backups: dict[Path, bytes | None] = {}
            try:
                for file_path, content in writes.items():
                    backups[file_path] = (
                        file_path.read_bytes() if file_path.exists() else None
                    )
                    file_path.parent.mkdir(parents=True, exist_ok=True)
                    file_path.write_text(content, encoding="utf-8")
            except Exception:
                # 回滚
                for file_path, original in backups.items():
                    if original is None:
                        if file_path.exists():
                            file_path.unlink()
                    else:
                        file_path.write_bytes(original)
                raise

            return "Patch applied:\n" + "\n".join(
                _format_summary(s) for s in summaries
            )

        except _PatchError as exc:
            return ToolResult.error(f"Error: {exc}")
        except Exception as exc:
            return ToolResult.error(f"Error applying patch: {exc}")

    # ------------------------------------------------------------------
    # add 操作
    # ------------------------------------------------------------------

    def _apply_add(
        self,
        edit: dict,
        source: Path,
        path: str,
        writes: dict[Path, str],
        summaries: list[_PatchSummary],
    ) -> None:
        """应用 add 操作：追加到现有文件或创建新文件。

        Args:
            edit: 编辑操作 dict。
            source: 解析后的文件 Path。
            path: 原始路径字符串。
            writes: 累积的写入映射。
            summaries: 累积的摘要列表。

        Raises:
            _PatchError: 缺少 new_text 或文件非 UTF-8。
        """
        new_text = edit.get("new_text")
        if new_text is None:
            raise _PatchError(f"new_text required for add: {path}")

        # 优先用已累积的内容（链式编辑）
        if source in writes:
            content = writes[source]
            exists = True
        elif source.exists():
            raw = source.read_bytes()
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError:
                raise _PatchError(f"file is not UTF-8 text: {path}")
            exists = True
        else:
            content = ""
            exists = False

        if exists:
            uses_crlf = "\r\n" in content
            new_norm = _append_text(content, new_text)
            if uses_crlf:
                new_norm = new_norm.replace("\n", "\r\n")
            writes[source] = new_norm
            added, deleted = _line_diff_stats(content, new_norm)
            action_name = "update"
        else:
            new_norm = new_text.replace("\r\n", "\n")
            if new_norm and not new_norm.endswith("\n"):
                new_norm += "\n"
            writes[source] = new_norm
            added = _text_line_count(new_norm)
            deleted = 0
            action_name = "add"

        summaries.append(
            _PatchSummary(action=action_name, path=path, added=added, deleted=deleted)
        )

    # ------------------------------------------------------------------
    # replace 操作
    # ------------------------------------------------------------------

    def _apply_replace(
        self,
        edit: dict,
        source: Path,
        path: str,
        writes: dict[Path, str],
        summaries: list[_PatchSummary],
    ) -> None:
        """应用 replace 操作：精确替换唯一匹配的 old_text。

        Args:
            edit: 编辑操作 dict。
            source: 解析后的文件 Path。
            path: 原始路径字符串。
            writes: 累积的写入映射。
            summaries: 累积的摘要列表。

        Raises:
            _PatchError: 缺少参数、文件不存在、old_text 不唯一。
        """
        old_text = edit.get("old_text") or ""
        if not old_text:
            raise _PatchError(f"old_text required for replace: {path}")

        new_text = edit.get("new_text")
        if new_text is None:
            raise _PatchError(f"new_text required for replace: {path}")

        # 优先用已累积的内容
        if source in writes:
            content = writes[source]
        elif source.exists():
            if not source.is_file():
                raise _PatchError(f"path to update is not a file: {path}")
            raw = source.read_bytes()
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError:
                raise _PatchError(f"file is not UTF-8 text: {path}")
        else:
            raise _PatchError(f"file to update does not exist: {path}")

        uses_crlf = "\r\n" in content
        norm_content = content.replace("\r\n", "\n")
        norm_old = old_text.replace("\r\n", "\n")

        pos = norm_content.find(norm_old)
        if pos < 0:
            raise _PatchError(f"old_text not found in {path}")
        if norm_content.find(norm_old, pos + 1) >= 0:
            raise _PatchError(f"old_text appears multiple times in {path}")

        new_norm = (
            norm_content[:pos]
            + new_text.replace("\r\n", "\n")
            + norm_content[pos + len(norm_old):]
        )
        if new_norm and not new_norm.endswith("\n"):
            new_norm += "\n"
        if uses_crlf:
            new_norm = new_norm.replace("\n", "\r\n")

        writes[source] = new_norm
        added, deleted = _line_diff_stats(content, new_norm)
        summaries.append(
            _PatchSummary(action="update", path=path, added=added, deleted=deleted)
        )
