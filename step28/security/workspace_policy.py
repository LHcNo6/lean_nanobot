"""Workspace 路径边界守卫。

对齐 nanobot `security/workspace_policy.py` 全量。

注意：这些是**应用级守卫**——它们在工具之间保持路径判定一致，但
不替代 OS sandbox（文档注释与 nanobot 原文一致）。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

#这是一个硬性策略边界，而非临时性故障，请勿使用 shell 技巧或替代工具进行重试如果该资源确实必不可少，请询问用户如何继续。
WORKSPACE_BOUNDARY_NOTE = (
    " (this is a hard policy boundary, not a transient failure; "
    "do not retry with shell tricks or alternative tools, and ask "
    "the user how to proceed if the resource is genuinely required)"
)


class WorkspaceBoundaryError(PermissionError):
    """请求的路径越过允许的 workspace 边界时抛出。"""


def resolve_path(path: str | Path, workspace: str | Path | None = None, *, strict: bool = False) -> Path:
    """解析 *path*；设置了 *workspace* 时相对路径按 workspace 解释。"""
    candidate = Path(path).expanduser()
    if not candidate.is_absolute() and workspace is not None:
        candidate = Path(workspace).expanduser() / candidate
    return candidate.resolve(strict=strict)


def _resolve_logical_path(path: str | Path, workspace: str | Path | None = None) -> Path:
    """返回绝对规范化路径但不跟随符号链接（用于精确文件匹配）。"""
    candidate = Path(path).expanduser()
    if not candidate.is_absolute() and workspace is not None:
        candidate = Path(workspace).expanduser() / candidate
    return Path(os.path.abspath(candidate))


def _path_key(path: str | Path) -> str:
    """路径比较键（规范化大小写，Windows 下不区分大小写）。"""
    return os.path.normcase(os.fspath(path))


def is_path_within(path: str | Path, root: str | Path) -> bool:
    """返回 *path* 是否解析为 *root* 或其子孙。"""
    try:
        resolved_path = Path(path).expanduser().resolve(strict=False)
        resolved_root = Path(root).expanduser().resolve(strict=False)
        resolved_path.relative_to(resolved_root)
        return True
    except (OSError, RuntimeError, TypeError, ValueError):
        return False


def is_path_allowed(path: str | Path, roots: Iterable[str | Path]) -> bool:
    """返回 *path* 是否位于任一允许根内。"""
    return any(is_path_within(path, root) for root in roots)


def _is_path_exactly_allowed(
    logical_path: Path,
    resolved_path: Path,
    files: Iterable[str | Path],
) -> bool:
    """返回 *path* 是否精确解析为允许文件之一（不要求它在根目录内）。"""
    logical_key = _path_key(logical_path)
    if _path_key(resolved_path) != logical_key:
        return False
    for file in files:
        try:
            allowed_file = _resolve_logical_path(file)
        except (OSError, RuntimeError, TypeError, ValueError):
            continue
        if _path_key(allowed_file) == logical_key:
            return True
    return False


def require_path_within(
    path: str | Path,
    root: str | Path,
    *,
    message: str | None = None,
) -> Path:
    """解析 *path* 并要求它在 *root* 内，否则抛 ``WorkspaceBoundaryError``。"""
    resolved = Path(path).expanduser().resolve(strict=False)
    if not is_path_within(resolved, root):
        raise WorkspaceBoundaryError(
            message
            or f"Path {path} is outside allowed directory {Path(root).expanduser()}"
            + WORKSPACE_BOUNDARY_NOTE
        )
    return resolved


def resolve_allowed_path(
    path: str | Path,
    *,
    workspace: str | Path | None = None,
    allowed_root: str | Path | None = None,
    extra_allowed_roots: Iterable[str | Path] | None = None,
    extra_allowed_files: Iterable[str | Path] | None = None,
    strict: bool = False,
) -> Path:
    """解析路径并（在配置了边界时）强制包含关系。

    语义（对齐 nanobot）：
    - ``allowed_root`` 与 ``extra_allowed_roots``：路径必须位于其中至少一个内；
    - ``extra_allowed_files``：允许精确命中这些文件（即使不在根内）；
    - 未配置任何根/文件时直接返回解析结果（不设边界）。
    """
    resolved = resolve_path(path, workspace, strict=False)
    files = list(extra_allowed_files or [])
    if allowed_root is None and not files:
        return resolve_path(path, workspace, strict=strict) if strict else resolved

    roots = []
    if allowed_root is not None:
        roots.append(allowed_root)
    roots.extend(extra_allowed_roots or [])
    exact_allowed = bool(files) and _is_path_exactly_allowed(
        _resolve_logical_path(path, workspace),
        resolved,
        files,
    )
    if not is_path_allowed(resolved, roots) and not exact_allowed:
        boundary = Path(allowed_root).expanduser() if allowed_root is not None else "allowed files"
        raise WorkspaceBoundaryError(
            f"Path {path} is outside allowed directory {boundary}"
            + WORKSPACE_BOUNDARY_NOTE
        )
    if strict:
        return resolve_path(path, workspace, strict=True)
    return resolved
