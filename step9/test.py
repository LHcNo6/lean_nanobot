"""Tests for Step 9 — MessageBus + events."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import Any

from step9.bus import MessageBus
from step9.consolidation import Consolidator, estimate_message_tokens, estimate_prompt_tokens
from step9.context import ContextBuilder
from step9.events import InboundMessage, OutboundMessage
from step9.llm import LLMResponse
from step9.session import Session, SessionManager


class _MockProvider:
    def __init__(self, response: LLMResponse | None = None):
        self._response = response

    @property
    def model(self) -> str:
        return "mock-model"

    async def chat_with_retry(self, **kwargs: Any) -> LLMResponse:
        if self._response is not None:
            return self._response
        return LLMResponse(
            content="Summary: user asked about weather, assistant provided forecast.",
            finish_reason="stop",
        )


class TestMessageBus(unittest.IsolatedAsyncioTestCase):
    async def test_publish_consume_inbound(self):
        bus = MessageBus()
        msg = InboundMessage(content="hello")
        await bus.publish_inbound(msg)
        received = await bus.consume_inbound()
        self.assertEqual(received.content, "hello")
        self.assertEqual(received.channel, "cli")

    async def test_publish_consume_outbound(self):
        bus = MessageBus()
        msg = OutboundMessage(content="world")
        await bus.publish_outbound(msg)
        received = await bus.consume_outbound()
        self.assertEqual(received.content, "world")

    async def test_multiple_messages_fifo(self):
        bus = MessageBus()
        for i in range(5):
            await bus.publish_inbound(InboundMessage(content=str(i)))
        for i in range(5):
            received = await bus.consume_inbound()
            self.assertEqual(received.content, str(i))

    async def test_inbound_size(self):
        bus = MessageBus()
        self.assertEqual(bus.inbound_size, 0)
        await bus.publish_inbound(InboundMessage(content="a"))
        self.assertEqual(bus.inbound_size, 1)
        await bus.publish_inbound(InboundMessage(content="b"))
        self.assertEqual(bus.inbound_size, 2)
        await bus.consume_inbound()
        self.assertEqual(bus.inbound_size, 1)

    async def test_outbound_size(self):
        bus = MessageBus()
        self.assertEqual(bus.outbound_size, 0)
        await bus.publish_outbound(OutboundMessage(content="x"))
        self.assertEqual(bus.outbound_size, 1)

    async def test_inbound_message_fields(self):
        msg = InboundMessage(
            content="test",
            channel="discord",
            sender_id="user1",
            chat_id="chat1",
            session_key="custom",
            metadata={"key": "val"},
        )
        self.assertEqual(msg.content, "test")
        self.assertEqual(msg.channel, "discord")
        self.assertEqual(msg.sender_id, "user1")
        self.assertEqual(msg.chat_id, "chat1")
        self.assertEqual(msg.session_key, "custom")
        self.assertEqual(msg.metadata, {"key": "val"})
        self.assertIsNotNone(msg.timestamp)

    async def test_outbound_message_fields(self):
        msg = OutboundMessage(content="reply", channel="telegram", chat_id="gid", metadata={"a": 1})
        self.assertEqual(msg.content, "reply")
        self.assertEqual(msg.channel, "telegram")
        self.assertEqual(msg.chat_id, "gid")
        self.assertEqual(msg.metadata, {"a": 1})

    async def test_concurrent_producer_consumer(self):
        bus = MessageBus()
        N = 100

        async def produce():
            for i in range(N):
                await bus.publish_inbound(InboundMessage(content=str(i)))

        async def consume():
            results = []
            for _ in range(N):
                msg = await bus.consume_inbound()
                results.append(int(msg.content))
            return results

        producer = asyncio.create_task(produce())
        consumer = asyncio.create_task(consume())
        await producer
        results = await consumer
        self.assertEqual(len(results), N)
        self.assertEqual(results, list(range(N)))

    async def test_agent_roundtrip(self):
        bus = MessageBus()
        provider = _MockProvider()
        registry = _MockToolRegistry()
        session_manager = SessionManager(workspace=tempfile.mkdtemp())
        context_builder = ContextBuilder(workspace=".")
        consolidator = Consolidator(provider=provider)
        identity = "You are a test bot."

        async def agent():
            msg = await bus.consume_inbound()
            session = session_manager.get_or_create("test")
            summary = await consolidator.maybe_consolidate(session, max_tokens=10000, model=provider.model)
            history = session.get_history(max_messages=50, max_tokens=10000)
            spec = AgentRunSpec(
                initial_messages=context_builder.build_messages(
                    current_message=msg.content, history=history, identity=identity, session_summary=summary,
                ),
                tools=registry,
                provider=provider,
            )
            result = await AgentRunner().run(spec)
            skip = 1 + len(history)
            session.import_messages(result.messages[skip:])
            session_manager.save(session)
            await bus.publish_outbound(OutboundMessage(content=result.final_content))

        agent_task = asyncio.create_task(agent())

        await bus.publish_inbound(InboundMessage(content="hello"))
        response = await bus.consume_outbound()
        self.assertIsNotNone(response.content)
        self.assertIn("Summary", response.content)

        agent_task.cancel()
        try:
            await agent_task
        except asyncio.CancelledError:
            pass


from step9.runner import AgentRunSpec, AgentRunner


class _MockToolRegistry:
    def get_definitions(self):
        return []

    async def execute(self, name, **params):
        return ""


# ── Existing step8 tests (unchanged) ──

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
        self.assertEqual(boundary, 0)

    def test_boundary_truncates(self):
        msgs = [{"role": "user", "content": "x" * 200} for _ in range(20)]
        boundary = Consolidator._find_boundary(msgs, 100)
        self.assertGreater(boundary, 0)
        self.assertLess(boundary, 20)


class TestConsolidatorMaybeConsolidate(unittest.IsolatedAsyncioTestCase):
    async def test_noop_when_under_budget(self):
        session = Session(key="test")
        session.add_message("user", "hi")
        conso = Consolidator()
        summary = await conso.maybe_consolidate(session, max_tokens=10000)
        self.assertIsNone(summary)
        self.assertEqual(session.last_consolidated, 0)

    async def test_truncate_without_provider(self):
        session = Session(key="test")
        for i in range(20):
            session.add_message("user", "x" * 200 + str(i))
        conso = Consolidator()
        summary = await conso.maybe_consolidate(session, max_tokens=100)
        self.assertIsNone(summary)
        self.assertGreater(session.last_consolidated, 0)

    async def test_with_provider_returns_summary(self):
        session = Session(key="test")
        for i in range(20):
            session.add_message("user", "x" * 200 + str(i))
        conso = Consolidator(provider=_MockProvider())
        summary = await conso.maybe_consolidate(session, max_tokens=100)
        self.assertIsNotNone(summary)
        self.assertIn("Summary", summary)
        self.assertIn("Summary", session.metadata.get("_last_summary", {}).get("text", ""))
        self.assertGreater(session.last_consolidated, 0)

    async def test_no_unconsolidated_messages(self):
        session = Session(key="test")
        session.last_consolidated = 0
        conso = Consolidator()
        result = await conso.maybe_consolidate(session, max_tokens=100)
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
        for h_msg in history:
            self.assertIn(h_msg, msgs)


if __name__ == "__main__":
    unittest.main()
