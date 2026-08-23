"""step118: build_dream_prompt 模板化测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from step118.memory import MemoryStore


@pytest.fixture
def store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(workspace=str(tmp_path))


class TestBuildDreamPromptTemplated:
    """build_dream_prompt 使用 _dream_template 测试。"""

    def test_uses_default_template_when_no_override(self, store: MemoryStore) -> None:
        """无覆盖时使用默认模板内容。"""
        store.append_history("test entry", session_key="test:1")
        result = store.build_dream_prompt()
        assert result is not None
        prompt, cursor = result
        # 默认模板包含 "Dream"
        assert "Dream" in prompt
        assert "Conversation History" in prompt

    def test_uses_override_template_when_exists(self, store: MemoryStore) -> None:
        """有覆盖时使用覆盖模板内容。"""
        store.dream_prompt_file.parent.mkdir(parents=True, exist_ok=True)
        store.dream_prompt_file.write_text("CUSTOM DREAM PROMPT", encoding="utf-8")
        store.append_history("test entry", session_key="test:1")
        result = store.build_dream_prompt()
        assert result is not None
        prompt, _ = result
        assert "CUSTOM DREAM PROMPT" in prompt

    def test_returns_none_when_no_history(self, store: MemoryStore) -> None:
        """无未处理历史时返回 None。"""
        result = store.build_dream_prompt()
        assert result is None

    def test_includes_memory_files_section(self, store: MemoryStore) -> None:
        """prompt 包含当前记忆文件内容。"""
        store.write_memory("test memory content")
        store.append_history("test entry", session_key="test:1")
        result = store.build_dream_prompt()
        assert result is not None
        prompt, _ = result
        assert "test memory content" in prompt

    def test_includes_history_entries(self, store: MemoryStore) -> None:
        """prompt 包含历史条目内容。"""
        store.append_history("important fact about user", session_key="test:1")
        result = store.build_dream_prompt()
        assert result is not None
        prompt, _ = result
        assert "important fact about user" in prompt

    def test_respects_max_entries(self, store: MemoryStore) -> None:
        """尊重 max_entries 参数。"""
        for i in range(30):
            store.append_history(f"entry {i}", session_key="test:1")
        result = store.build_dream_prompt(max_entries=5)
        assert result is not None
        prompt, cursor = result
        # 只处理前 5 条，cursor 应为 5
        assert cursor == 5
