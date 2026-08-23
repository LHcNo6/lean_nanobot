"""Git-backed version control for memory files.

对齐 nanobot ``utils/gitstore.py``，使用 subprocess 调用 git 命令
（参考实现使用 dulwich，此处为最小增量实现，接口兼容）。
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_WORKING_TREE_DIFF_MAX_CHARS = 6000


@dataclass
class CommitInfo:
    """提交信息。"""

    sha: str  # Short SHA (8 chars)
    message: str
    timestamp: str  # Formatted datetime

    def subject(self) -> str:
        """返回 commit message 第一行，空时返回占位符。"""
        lines = self.message.splitlines()
        return lines[0] if lines else "(no message)"


class GitStore:
    """Git-backed version control for memory files."""

    def __init__(self, workspace: Path, tracked_files: list[str]):
        self._workspace = Path(workspace)
        self._tracked_files = tracked_files

    def _run_git(self, *args: str, check: bool = False) -> subprocess.CompletedProcess:
        """运行 git 命令。"""
        return subprocess.run(
            ["git", *args],
            cwd=str(self._workspace),
            capture_output=True,
            text=True,
            check=check,
        )

    def is_initialized(self) -> bool:
        """检查 git 仓库是否已初始化。"""
        return (self._workspace / ".git").is_dir()

    def init(self) -> bool:
        """初始化 git 仓库（如未初始化）。

        创建 .gitignore 并做初始提交。新建返回 True，已存在返回 False。
        """
        if self.is_initialized():
            return False

        try:
            self._run_git("init", check=True)
            # 配置 git 用户（避免 commit 失败）
            self._run_git("config", "user.name", "nanobot")
            self._run_git("config", "user.email", "nanobot@dream")

            # 写 .gitignore（简单版本，不使用 * 通配）
            gitignore = self._workspace / ".gitignore"
            if not gitignore.exists():
                gitignore.write_text(self._build_gitignore(), encoding="utf-8")

            # 确保 tracked 文件存在
            for rel in self._tracked_files:
                p = self._workspace / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                if not p.exists():
                    p.write_text("", encoding="utf-8")

            # 初始提交
            self._run_git("add", ".gitignore", *self._tracked_files, check=True)
            self._run_git("commit", "-m", "init: nanobot memory store", check=True)
            logger.info("Git store initialized at %s", self._workspace)
            return True
        except Exception:
            logger.exception("Git store init failed for %s", self._workspace)
            return False

    def auto_commit(self, message: str) -> str | None:
        """暂存 tracked 记忆文件并提交（如有变更）。

        返回短 commit SHA，无变更返回 None。
        """
        if not self.is_initialized():
            return None

        try:
            # 检查是否有变更
            status = self._run_git("status", "--porcelain", *self._tracked_files)
            if not status.stdout.strip():
                return None

            self._run_git("add", *self._tracked_files, check=True)
            result = self._run_git("commit", "-m", message, check=True)
            # 获取 SHA
            sha_result = self._run_git("rev-parse", "--short=8", "HEAD")
            sha = sha_result.stdout.strip()
            if not sha:
                return None
            logger.debug("Git auto-commit: %s (%s)", sha, message)
            return sha
        except Exception:
            logger.exception("Git auto-commit failed: %s", message)
            return None

    def summarize_working_tree(self) -> str:
        """返回工作区 tracked 文件变更的结构化摘要。

        格式：每行一个文件的变更状态（A/M/D）+ 路径。
        """
        if not self.is_initialized():
            return ""
        try:
            result = self._run_git("status", "--porcelain", *self._tracked_files)
            lines = []
            for line in result.stdout.splitlines():
                if line.strip():
                    lines.append(line.strip())
            return "\n".join(lines)
        except Exception:
            return ""

    def _build_gitignore(self) -> str:
        """构建默认 .gitignore 内容。"""
        return "# nanobot memory store\n" + "".join(f"!{f}\n" for f in self._tracked_files)
