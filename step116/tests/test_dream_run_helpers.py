"""step116: dream_run_completed + build_dream_commit_message 测试。"""

from __future__ import annotations

from types import SimpleNamespace

from step116.memory import MemoryStore


class TestDreamRunCompleted:
    """dream_run_completed 测试。"""

    def test_none_returns_false(self) -> None:
        """None 返回 False。"""
        assert MemoryStore.dream_run_completed(None) is False

    def test_no_metadata_returns_false(self) -> None:
        """无 metadata 返回 False。"""
        resp = SimpleNamespace()
        assert MemoryStore.dream_run_completed(resp) is False

    def test_metadata_not_dict_returns_false(self) -> None:
        """metadata 非 dict 返回 False。"""
        resp = SimpleNamespace(metadata="not a dict")
        assert MemoryStore.dream_run_completed(resp) is False

    def test_wrong_stop_reason_returns_false(self) -> None:
        """_stop_reason 不是 completed 返回 False。"""
        resp = SimpleNamespace(metadata={"_stop_reason": "max_iterations"})
        assert MemoryStore.dream_run_completed(resp) is False

    def test_completed_returns_true(self) -> None:
        """_stop_reason == completed 返回 True。"""
        resp = SimpleNamespace(metadata={"_stop_reason": "completed"})
        assert MemoryStore.dream_run_completed(resp) is True

    def test_missing_stop_reason_returns_false(self) -> None:
        """metadata 中无 _stop_reason 返回 False。"""
        resp = SimpleNamespace(metadata={"other": "value"})
        assert MemoryStore.dream_run_completed(resp) is False


class TestBuildDreamCommitMessage:
    """build_dream_commit_message 测试。"""

    def test_empty_diff_returns_prefix(self) -> None:
        """空 diff_body 返回纯 prefix。"""
        result = MemoryStore.build_dream_commit_message("dream: update memory", "")
        assert result == "dream: update memory"

    def test_none_diff_returns_prefix(self) -> None:
        """None diff_body 返回纯 prefix。"""
        result = MemoryStore.build_dream_commit_message("dream: update memory", None)
        assert result == "dream: update memory"

    def test_whitespace_diff_returns_prefix(self) -> None:
        """纯空白 diff_body 返回纯 prefix。"""
        result = MemoryStore.build_dream_commit_message("dream: update memory", "   \n  ")
        assert result == "dream: update memory"

    def test_with_diff_body(self) -> None:
        """有 diff_body 时 prefix + 空行 + diff_body。"""
        diff = "- MEMORY.md: added user preference\n- USER.md: updated name"
        result = MemoryStore.build_dream_commit_message("dream: update memory", diff)
        assert result.startswith("dream: update memory")
        assert "\n\n" in result
        assert diff in result

    def test_diff_body_stripped(self) -> None:
        """diff_body 首尾空白被去除。"""
        diff = "  actual diff  "
        result = MemoryStore.build_dream_commit_message("prefix", diff)
        assert result == "prefix\n\nactual diff"
