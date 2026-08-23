"""step118: 子代理 microcompaction 工具集对齐测试。

验证 `list_exec_sessions` 进入 `COMPACTABLE_TOOLS`，使其长结果被 `ContextGovernor` 选为
inflight 微压缩候选（对齐 nanobot）。同时验证非可压缩工具（如 `spawn`）不入选。

全部测试使用构造数据，禁止真实网络与 API 调用。
"""

from __future__ import annotations

import unittest

from step118.governance import (
    COMPACTABLE_TOOLS,
    ContextGovernanceConfig,
    ContextGovernor,
    MICROCOMPACT_MIN_CHARS,
)


def _config() -> ContextGovernanceConfig:
    """最小治理配置：仅 inflight_start_index=0，tools 不参与候选选择。"""
    return ContextGovernanceConfig(tools=None, inflight_start_index=0)


def _tool_result(name: str, content: str, call_id: str = "c1") -> dict:
    """构造一条 role=tool 的消息。"""
    return {"role": "tool", "tool_call_id": call_id, "name": name, "content": content}


class TestCompactableToolsSet(unittest.TestCase):
    """D1：list_exec_sessions 在可压缩集合内。"""

    def test_list_exec_sessions_is_compactable(self) -> None:
        self.assertIn("list_exec_sessions", COMPACTABLE_TOOLS)

    def test_existing_tools_preserved(self) -> None:
        """既有 6 项不被误删。"""
        for name in ("exec", "grep", "find_files", "web_search", "web_fetch", "list_dir"):
            self.assertIn(name, COMPACTABLE_TOOLS)


class TestInflightCompactionCandidates(unittest.IsolatedAsyncioTestCase):
    """D2：list_exec_sessions 长结果入选候选；非可压缩工具不入选。"""

    def test_list_exec_sessions_long_result_is_candidate(self) -> None:
        """list_exec_sessions 长结果（≥500 字）应被选为微压缩候选。"""
        gov = ContextGovernor()
        msg = _tool_result("list_exec_sessions", "x" * (MICROCOMPACT_MIN_CHARS + 10))
        candidates = gov._inflight_compaction_candidates(_config(), [msg], set())
        self.assertEqual(candidates, [(0, "c1")])

    def test_short_result_not_candidate(self) -> None:
        """短结果（<500 字）即便工具可压缩也不入选。"""
        gov = ContextGovernor()
        msg = _tool_result("list_exec_sessions", "x" * 10)
        candidates = gov._inflight_compaction_candidates(_config(), [msg], set())
        self.assertEqual(candidates, [])

    def test_non_compactable_tool_excluded(self) -> None:
        """非可压缩工具（spawn）的长结果不入选。"""
        gov = ContextGovernor()
        msg = _tool_result("spawn", "x" * (MICROCOMPACT_MIN_CHARS + 10))
        candidates = gov._inflight_compaction_candidates(_config(), [msg], set())
        self.assertEqual(candidates, [])

    def test_already_compacted_excluded(self) -> None:
        """已压缩过的 tool_call_id 不再重复入选。"""
        gov = ContextGovernor()
        msg = _tool_result("list_exec_sessions", "x" * (MICROCOMPACT_MIN_CHARS + 10))
        candidates = gov._inflight_compaction_candidates(_config(), [msg], {"c1"})
        self.assertEqual(candidates, [])


if __name__ == "__main__":
    unittest.main()
