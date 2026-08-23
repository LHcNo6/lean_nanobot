"""step125：通道清洗（G6 通道部分）测试。

验证：
1. `scrub_subagent_announce_body`：保留 [Subagent 头 + 截断 Result 正文，
   移除 Task: / Summarize 脚手架；超长 Result 截断；缺失 Result 段兜底。
2. `scrub_subagent_messages_for_channel`：原地改写 subagent_result 消息。
3. `/history` 展示边界：subagent_result 行不含脚手架文本，含结果片段。

全部使用构造数据，无真实网络/API 调用。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from step125.command.builtin import _cmd_history
from step125.command.router import CommandContext
from step125.utils.subagent_channel_display import (
    scrub_subagent_announce_body,
    scrub_subagent_messages_for_channel,
)

_FULL_ANNOUNCE = (
    "[Subagent 'research' completed successfully]\n\n"
    "Task: Research the capital of France\n\n"
    "Result:\n"
    "The capital of France is Paris.\n\n"
    'Summarize this naturally for the user. Keep it brief (1-2 sentences). '
    'Do not mention technical details like "subagent" or task IDs.'
)


class TestScrubAnnounceBody(unittest.TestCase):
    """F1：scrub_subagent_announce_body。"""

    def test_removes_scaffolding_keeps_header_and_result(self) -> None:
        out = scrub_subagent_announce_body(_FULL_ANNOUNCE)
        self.assertIn("[Subagent 'research' completed successfully]", out)
        self.assertIn("The capital of France is Paris.", out)
        self.assertNotIn("Task:", out)
        self.assertNotIn("Summarize this naturally", out)

    def test_long_result_truncated(self) -> None:
        long_result = "Result is " + ("x" * 5000)
        announce = (
            "[Subagent 'big' completed successfully]\n\n"
            "Task: do big thing\n\n"
            f"Result:\n{long_result}\n\n"
            "Summarize this naturally for the user."
        )
        out = scrub_subagent_announce_body(announce)
        # body 上限 800，整体远小于 5000+ 原文。
        self.assertLess(len(out), 900)
        self.assertNotIn("Task:", out)

    def test_no_result_section_falls_back_to_header(self) -> None:
        announce = "[Subagent 'x' done]\n\nSome random note without result section"
        out = scrub_subagent_announce_body(announce)
        self.assertEqual(out, "[Subagent 'x' done]")

    def test_does_not_mutate_input(self) -> None:
        original = _FULL_ANNOUNCE
        _ = scrub_subagent_announce_body(original)
        self.assertEqual(original, _FULL_ANNOUNCE)


class TestScrubMessagesForChannel(unittest.TestCase):
    """F1：scrub_subagent_messages_for_channel 原地改写。"""

    def test_scrubs_only_subagent_result(self) -> None:
        messages = [
            {"role": "user", "content": "Task: should stay", "injected_event": "none"},
            {
                "role": "assistant",
                "content": _FULL_ANNOUNCE,
                "injected_event": "subagent_result",
            },
        ]
        scrub_subagent_messages_for_channel(messages)
        self.assertEqual(messages[0]["content"], "Task: should stay")
        self.assertNotIn("Task:", messages[1]["content"])
        self.assertIn("The capital of France is Paris.", messages[1]["content"])
        self.assertNotIn("Summarize this naturally", messages[1]["content"])


class TestHistoryCommandScrubs(unittest.IsolatedAsyncioTestCase):
    """F2：/history 展示边界对 subagent_result 行做清洗。"""

    async def test_history_hides_scaffolding(self) -> None:
        session = SimpleNamespace(
            messages=[
                {"role": "user", "content": "hello"},
                {
                    "role": "assistant",
                    "content": _FULL_ANNOUNCE,
                    "injected_event": "subagent_result",
                },
            ],
            last_consolidated=-1,
            metadata={},
        )
        ctx = CommandContext(msg=None, session=session, key="cli:c", raw="/history")
        out = await _cmd_history(ctx)
        self.assertNotIn("Task:", out.content)
        self.assertNotIn("Summarize this naturally", out.content)
        self.assertIn("Paris", out.content)


if __name__ == "__main__":
    unittest.main()
