"""step116: get_memory_context + context.py 集成测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from step116.context import ContextBuilder
from step116.memory import MemoryStore


class TestGetMemoryContext:
    """MemoryStore.get_memory_context 测试。"""

    def test_empty_memory_returns_empty(self, tmp_path: Path) -> None:
        """MEMORY.md 不存在时返回空串。"""
        store = MemoryStore(workspace=str(tmp_path))
        assert store.get_memory_context() == ""

    def test_blank_memory_returns_empty(self, tmp_path: Path) -> None:
        """MEMORY.md 为空文件时返回空串。"""
        store = MemoryStore(workspace=str(tmp_path))
        store.write_memory("")
        assert store.get_memory_context() == ""

    def test_memory_with_content(self, tmp_path: Path) -> None:
        """MEMORY.md 有内容时返回格式化字符串。"""
        store = MemoryStore(workspace=str(tmp_path))
        store.write_memory("- 用户偏好中文回复\n- 项目使用 Python")
        result = store.get_memory_context()
        assert result == "## Long-term Memory\n- 用户偏好中文回复\n- 项目使用 Python"

    def test_memory_preserves_newlines(self, tmp_path: Path) -> None:
        """多行记忆内容保持换行。"""
        store = MemoryStore(workspace=str(tmp_path))
        store.write_memory("line1\nline2\nline3")
        result = store.get_memory_context()
        assert "line1" in result
        assert "line2" in result
        assert "line3" in result


class TestContextBuilderMemory:
    """ContextBuilder 惰性 memory 属性测试。"""

    def test_memory_lazy_init(self, tmp_path: Path) -> None:
        """首次访问 memory 属性时创建 MemoryStore。"""
        builder = ContextBuilder(workspace=str(tmp_path))
        assert builder._memory is None
        mem = builder.memory
        assert isinstance(mem, MemoryStore)
        assert builder._memory is not None

    def test_memory_cached(self, tmp_path: Path) -> None:
        """多次访问返回同一实例。"""
        builder = ContextBuilder(workspace=str(tmp_path))
        mem1 = builder.memory
        mem2 = builder.memory
        assert mem1 is mem2

    def test_memory_uses_workspace(self, tmp_path: Path) -> None:
        """MemoryStore 使用 ContextBuilder 的 workspace。"""
        builder = ContextBuilder(workspace=str(tmp_path))
        assert builder.memory.workspace == tmp_path


class TestBuildSystemPromptMemoryInjection:
    """build_system_prompt 中长期记忆注入测试。"""

    def test_include_memory_injects_long_term(self, tmp_path: Path) -> None:
        """include_memory_recent_history=True 且 MEMORY.md 有内容时注入。"""
        builder = ContextBuilder(workspace=str(tmp_path))
        builder.memory.write_memory("- 测试记忆条目")
        prompt = builder.build_system_prompt(include_memory_recent_history=True)
        assert "## Long-term Memory" in prompt
        assert "测试记忆条目" in prompt

    def test_exclude_memory_no_injection(self, tmp_path: Path) -> None:
        """include_memory_recent_history=False 时不注入。"""
        builder = ContextBuilder(workspace=str(tmp_path))
        builder.memory.write_memory("- 测试记忆条目")
        prompt = builder.build_system_prompt(include_memory_recent_history=False)
        assert "## Long-term Memory" not in prompt
        assert "测试记忆条目" not in prompt

    def test_empty_memory_no_injection(self, tmp_path: Path) -> None:
        """MEMORY.md 为空时不注入记忆段。"""
        builder = ContextBuilder(workspace=str(tmp_path))
        prompt = builder.build_system_prompt(include_memory_recent_history=True)
        assert "## Long-term Memory" not in prompt

    def test_memory_injection_position(self, tmp_path: Path) -> None:
        """长期记忆段位于 bootstrap 文件之后、skills 之前。"""
        # 创建一个 bootstrap 文件
        (tmp_path / "SOUL.md").write_text("test soul", encoding="utf-8")
        builder = ContextBuilder(
            workspace=str(tmp_path),
            bootstrap_files=["SOUL.md"],
        )
        builder.memory.write_memory("- 记忆")
        prompt = builder.build_system_prompt(include_memory_recent_history=True)
        soul_idx = prompt.find("## SOUL.md")
        memory_idx = prompt.find("## Long-term Memory")
        assert soul_idx < memory_idx, "长期记忆应在 bootstrap 文件之后"

    def test_default_include_memory_true(self, tmp_path: Path) -> None:
        """默认 include_memory_recent_history=True。"""
        builder = ContextBuilder(workspace=str(tmp_path))
        builder.memory.write_memory("- 默认注入测试")
        prompt = builder.build_system_prompt()
        assert "## Long-term Memory" in prompt
