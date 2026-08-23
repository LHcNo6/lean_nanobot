"""workspace 本地 prompt 覆盖文件的共享处理。

对齐 nanobot ``utils/workspace_prompts.py``。
"""

from __future__ import annotations

from contextlib import suppress
from pathlib import Path

from step123.helpers import truncate_text

WORKSPACE_PROMPT_MAX_CHARS = 32_000


def workspace_prompt_file(workspace: Path, name: str) -> Path:
    """返回命名 workspace prompt 覆盖文件的约定路径。"""
    return workspace / "prompts" / f"{name}.md"


def load_workspace_prompt_override(
    path: Path,
    *,
    max_chars: int = WORKSPACE_PROMPT_MAX_CHARS,
) -> tuple[str | None, int]:
    """加载并截断非空 UTF-8 prompt 覆盖文件。

    返回 (加载的文本, 原始长度)。缺失、不可读或空文件返回 (None, 0)，
    调用方可据此回退到默认 prompt。
    """
    with suppress(OSError, UnicodeDecodeError):
        text = path.read_text(encoding="utf-8").rstrip()
        if text:
            original_chars = len(text)
            return truncate_text(text, max_chars), original_chars
    return None, 0


def has_workspace_prompt_override(path: Path) -> bool:
    """返回路径是否包含非空 workspace prompt 覆盖文件。"""
    text, _original_chars = load_workspace_prompt_override(path)
    return text is not None


def initialize_workspace_prompt(path: Path, default_prompt: str) -> bool:
    """当目标缺失或为空时创建默认 prompt 副本。

    非空文件、非文件路径或无法安全读取的路径返回 False 且不覆盖。
    """
    try:
        if path.exists() and (
            not path.is_file() or bool(path.read_text(encoding="utf-8").strip())
        ):
            return False
    except (OSError, UnicodeDecodeError):
        return False

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(default_prompt + "\n", encoding="utf-8")
    return True
