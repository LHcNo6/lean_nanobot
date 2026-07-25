"""Tests for Step 8 — Token-aware consolidation."""

import tempfile
import unittest
from pathlib import Path
from typing import Any

from step8.consolidation import (
    Consolidator,
    estimate_message_tokens,
    estimate_prompt_tokens,
)
from step8.context import ContextBuilder
from step8.llm import LLMResponse
from step8.session import Session, SessionManager


class _MockProvider:
    def __init__(self, response: LLMResponse | None = None):
        self._response = response

    async def chat_with_retry(self, **kwargs: Any) -> LLMResponse:
        if self._response is not None:
            return self._response
        return LLMResponse(
            content="Summary: user asked about weather, assistant provided forecast.",
            finish_reason="stop",
        )


class TestTokenEstimator(unittest.TestCase):
    def test_text_message(self):
        tokens = estimate_message_tokens({"role": "user", "content": "hello"})
        self.assertGreaterEqual(tokens, 4)

    def test_long_text(self):
        tokens = estimate_message_tokens({"role": "user", "content": "a" * 100})
        self.assertGreaterEqual(tokens, 25)

    def test_with_tool_calls(self):
        msg = {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "echo", "arguments": '{"text":"hi"}'}}],
        }
        tokens = estimate_message_tokens(msg)
        self.assertGreater(tokens, 10)

    def test_with_tool_result(self):
        msg = {"role": "tool", "content": "Echo: hi", "name": "echo", "tool_call_id": "c1"}
        tokens = estimate_message_tokens(msg)
        self.assertGreaterEqual(tokens, 4)

    def test_estimate_prompt_tokens(self):
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        total = estimate_prompt_tokens(msgs)
        self.assertGreaterEqual(total, 8)


class TestGetHistoryMaxTokens(unittest.TestCase):
    def test_max_tokens_limits_history(self):
        session = Session(key="test")
        for i in range(20):
            session.add_message("user", f"message {i}")
        # Each "message N" is ~9 chars ≈ 2-3 tokens + 4 overhead ≈ 6-7 tokens
        # 20 messages should be > 50 tokens
        history = session.get_history(max_messages=50, max_tokens=50)
        self.assertLess(len(history), 20)
        self.assertGreater(len(history), 0)

    def test_max_tokens_returns_all_if_under_budget(self):
        session = Session(key="test")
        session.add_message("user", "hi")
        history = session.get_history(max_messages=50, max_tokens=10000)
        self.assertEqual(len(history), 1)

    def test_max_tokens_respects_last_consolidated(self):
        session = Session(key="test")
        for i in range(10):
            session.add_message("user", str(i))
        session.last_consolidated = 8
        history = session.get_history(max_messages=50, max_tokens=100)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["content"], "8")

    def test_max_tokens_zero_behavior(self):
        session = Session(key="test")
        for i in range(5):
            session.add_message("user", str(i))
        history = session.get_history(max_messages=50, max_tokens=0)
        self.assertEqual(len(history), 5)


class TestConsolidatorFindBoundary(unittest.TestCase):
    def test_boundary_under_target(self):
        msgs = [{"role": "user", "content": f"msg{i}"} for i in range(10)]
        boundary = Consolidator._find_boundary(msgs, 10000)
        self.assertEqual(boundary, 0)  # all fit, no consolidation

    def test_boundary_truncates(self):
        msgs = [{"role": "user", "content": "x" * 200} for _ in range(20)]
        boundary = Consolidator._find_boundary(msgs, 100)
        self.assertGreater(boundary, 0)
        self.assertLess(boundary, 20)


class TestConsolidatorMaybeConsolidate(unittest.TestCase):
    def test_noop_when_under_budget(self):
        session = Session(key="test")
        session.add_message("user", "hi")
        conso = Consolidator()
        result = conso.maybe_consolidate(session, max_tokens=10000)
        # synchronous, no await needed since provider is None
        import asyncio
        summary = asyncio.run(result)
        self.assertIsNone(summary)
        self.assertEqual(session.last_consolidated, 0)

    def test_truncate_without_provider(self):
        session = Session(key="test")
        for i in range(20):
            session.add_message("user", "x" * 200 + str(i))
        conso = Consolidator()
        import asyncio
        summary = asyncio.run(conso.maybe_consolidate(session, max_tokens=100))
        self.assertIsNone(summary)  # no provider → no summary
        self.assertGreater(session.last_consolidated, 0)

    def test_with_provider_returns_summary(self):
        session = Session(key="test")
        for i in range(20):
            session.add_message("user", "x" * 200 + str(i))
        conso = Consolidator(provider=_MockProvider())
        import asyncio
        summary = asyncio.run(conso.maybe_consolidate(session, max_tokens=100))
        self.assertIsNotNone(summary)
        self.assertIn("Summary", summary)
        self.assertIn("Summary", session.metadata.get("_last_summary", {}).get("text", ""))
        self.assertGreater(session.last_consolidated, 0)

    def test_no_unconsolidated_messages(self):
        session = Session(key="test")
        session.last_consolidated = 0
        conso = Consolidator()
        import asyncio
        result = asyncio.run(conso.maybe_consolidate(session, max_tokens=100))
        self.assertIsNone(result)


class TestFormatMessages(unittest.TestCase):
    def test_format_simple(self):
        msgs = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
        formatted = Consolidator._format_messages(msgs)
        self.assertIn("[user]", formatted)
        self.assertIn("[assistant]", formatted)
        self.assertIn("hi", formatted)
        self.assertIn("hello", formatted)

    def test_format_with_tool_calls(self):
        msgs = [{"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]}]
        formatted = Consolidator._format_messages(msgs)
        self.assertIn("[tool_calls:", formatted)


class TestContextBuilderWithSummary(unittest.TestCase):
    def test_session_summary_in_system_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = ContextBuilder(workspace=tmp)
            prompt = ctx.build_system_prompt(session_summary="User likes Python.")
        self.assertIn("Archived Context Summary", prompt)
        self.assertIn("User likes Python.", prompt)

    def test_session_summary_in_build_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = ContextBuilder(workspace=tmp)
            msgs = ctx.build_messages("Hi", session_summary="User likes Go.")
        self.assertIn("Archived Context Summary", msgs[0]["content"])
        self.assertIn("User likes Go.", msgs[0]["content"])

    def test_no_summary_when_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = ContextBuilder(workspace=tmp)
            prompt = ctx.build_system_prompt()
        self.assertNotIn("Archived Context Summary", prompt)


class TestIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_consolidation_in_full_flow(self):
        """Consolidator + get_history(max_tokens) + session_summary in context."""
        session = Session(key="integration")
        for i in range(20):
            session.add_message("user", "x" * 200 + str(i))
            session.add_message("assistant", "y" * 100 + str(i))

        conso = Consolidator(provider=_MockProvider())
        summary = await conso.maybe_consolidate(session, max_tokens=200)
        self.assertIsNotNone(summary)
        self.assertGreater(session.last_consolidated, 0)

        history = session.get_history(max_messages=50, max_tokens=200)
        self.assertGreater(len(history), 0)
        self.assertLess(len(history), 40)

        with tempfile.TemporaryDirectory() as tmp:
            ctx = ContextBuilder(workspace=tmp)
            msgs = ctx.build_messages("Continue", history=history, session_summary=summary)
        self.assertIn("Archived Context Summary", msgs[0]["content"])
        self.assertEqual(msgs[-1]["content"], "Continue")
        # history appears between system and user
        for h_msg in history:
            self.assertIn(h_msg, msgs)


if __name__ == "__main__":
    unittest.main()
