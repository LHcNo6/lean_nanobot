"""step106: workspace_prompts 模块 + MemoryStore dream 模板方法测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from step106.memory import MemoryStore
from step106.utils.workspace_prompts import (
    WORKSPACE_PROMPT_MAX_CHARS,
    has_workspace_prompt_override,
    initialize_workspace_prompt,
    load_workspace_prompt_override,
    workspace_prompt_file,
)


@pytest.fixture
def store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(workspace=str(tmp_path))


class TestWorkspacePrompts:
    """workspace_prompts 模块测试。"""

    def test_workspace_prompt_file_path(self, tmp_path: Path) -> None:
        """返回正确的 prompts/name.md 路径。"""
        path = workspace_prompt_file(tmp_path, "dream")
        assert path == tmp_path / "prompts" / "dream.md"

    def test_load_missing_returns_none(self, tmp_path: Path) -> None:
        """缺失文件返回 (None, 0)。"""
        text, chars = load_workspace_prompt_override(tmp_path / "missing.md")
        assert text is None
        assert chars == 0

    def test_load_empty_returns_none(self, tmp_path: Path) -> None:
        """空文件返回 (None, 0)。"""
        p = tmp_path / "empty.md"
        p.write_text("   \n  ", encoding="utf-8")
        text, chars = load_workspace_prompt_override(p)
        assert text is None
        assert chars == 0

    def test_load_nonempty_returns_text(self, tmp_path: Path) -> None:
        """非空文件返回截断后文本和原始长度。"""
        p = tmp_path / "test.md"
        content = "hello world"
        p.write_text(content, encoding="utf-8")
        text, chars = load_workspace_prompt_override(p)
        assert text == content
        assert chars == len(content)

    def test_load_truncates_long_text(self, tmp_path: Path) -> None:
        """超长文本被截断。"""
        p = tmp_path / "long.md"
        content = "a" * (WORKSPACE_PROMPT_MAX_CHARS + 100)
        p.write_text(content, encoding="utf-8")
        text, chars = load_workspace_prompt_override(p)
        assert text is not None
        assert len(text) < len(content)
        assert chars == len(content)

    def test_has_override_false_for_missing(self, tmp_path: Path) -> None:
        """缺失文件返回 False。"""
        assert has_workspace_prompt_override(tmp_path / "missing.md") is False

    def test_has_override_true_for_nonempty(self, tmp_path: Path) -> None:
        """非空文件返回 True。"""
        p = tmp_path / "test.md"
        p.write_text("content", encoding="utf-8")
        assert has_workspace_prompt_override(p) is True

    def test_initialize_creates_default(self, tmp_path: Path) -> None:
        """缺失时创建默认 prompt。"""
        p = tmp_path / "prompts" / "dream.md"
        result = initialize_workspace_prompt(p, "default content")
        assert result is True
        assert p.read_text(encoding="utf-8").strip() == "default content"

    def test_initialize_does_not_overwrite(self, tmp_path: Path) -> None:
        """非空文件不被覆盖。"""
        p = tmp_path / "prompts" / "dream.md"
        p.parent.mkdir(parents=True)
        p.write_text("existing", encoding="utf-8")
        result = initialize_workspace_prompt(p, "new content")
        assert result is False
        assert p.read_text(encoding="utf-8") == "existing"


class TestMemoryStoreDreamTemplate:
    """MemoryStore dream 模板方法测试。"""

    def test_dream_prompt_file_path(self, store: MemoryStore) -> None:
        """dream_prompt_file 返回正确路径。"""
        assert store.dream_prompt_file == store.workspace / "prompts" / "dream.md"

    def test_has_dream_prompt_override_false_default(self, store: MemoryStore) -> None:
        """默认无覆盖文件。"""
        assert store.has_dream_prompt_override() is False

    def test_has_dream_prompt_override_true(self, store: MemoryStore) -> None:
        """存在覆盖文件时返回 True。"""
        store.dream_prompt_file.parent.mkdir(parents=True, exist_ok=True)
        store.dream_prompt_file.write_text("custom dream", encoding="utf-8")
        assert store.has_dream_prompt_override() is True

    def test_default_dream_prompt_not_empty(self) -> None:
        """默认 dream prompt 非空。"""
        text = MemoryStore.default_dream_prompt()
        assert text
        assert "Dream" in text

    def test_dream_template_uses_default_when_no_override(self, store: MemoryStore) -> None:
        """无覆盖时返回默认 prompt。"""
        result = store._dream_template()
        assert result == MemoryStore.default_dream_prompt()

    def test_dream_template_uses_override_when_exists(self, store: MemoryStore) -> None:
        """有覆盖时返回覆盖内容。"""
        store.dream_prompt_file.parent.mkdir(parents=True, exist_ok=True)
        store.dream_prompt_file.write_text("custom dream prompt", encoding="utf-8")
        result = store._dream_template()
        assert result == "custom dream prompt"

    def test_dream_template_oversize_logged_once(self, store: MemoryStore) -> None:
        """超长覆盖只警告一次。"""
        store.dream_prompt_file.parent.mkdir(parents=True, exist_ok=True)
        long_content = "x" * (WORKSPACE_PROMPT_MAX_CHARS + 100)
        store.dream_prompt_file.write_text(long_content, encoding="utf-8")
        # 第一次调用设置 flag
        result1 = store._dream_template()
        assert store._dream_prompt_oversize_logged is True
        # 第二次调用不重复警告（flag 已设置）
        result2 = store._dream_template()
        assert result1 == result2
