"""Tests for Step 10 — AgentLoop state machine."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import Any

from step10.bus import MessageBus
from step10.consolidation import Consolidator, estimate_message_tokens, estimate_prompt_tokens
from step10.context import ContextBuilder
from step10.events import InboundMessage, OutboundMessage
from step10.llm import LLMResponse
from step10.loop import AgentLoop, TurnContext, TurnState
from step10.session import Session, SessionManager


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


class _MockToolRegistry:
    def get_definitions(self):
        return []

    async def execute(self, name, **params):
        return ""


from step10.runner import AgentRunSpec, AgentRunner


def _make_loop(response=None, consolidator_provider=None, replay_budget=10000):
    bus = MessageBus()
    provider = _MockProvider(response=response)
    registry = _MockToolRegistry()
    tmp = tempfile.mkdtemp()
    session_manager = SessionManager(workspace=tmp)
    context_builder = ContextBuilder(workspace=".")
    consolidator = Consolidator(provider=consolidator_provider)
    loop = AgentLoop(
        bus=bus, provider=provider, registry=registry,
        session_manager=session_manager, context_builder=context_builder,
        consolidator=consolidator, identity="You are a test bot.",
        replay_budget=replay_budget,
    )
    return loop, bus


def _run_loop(loop):
    task = asyncio.create_task(loop.run())
    return task


class TestAgentLoopStateHandlers(unittest.IsolatedAsyncioTestCase):
    """Unit tests for individual state handlers (no loop.run needed)."""

    async def test_state_restore(self):
        loop, _ = _make_loop()
        ctx = TurnContext(msg=InboundMessage(content="hi", chat_id="test"), session_key="test")
        event = await loop._state_restore(ctx)
        self.assertEqual(event, "ok")
        self.assertIsNotNone(ctx.session)
        self.assertEqual(ctx.session.key, "test")

    async def test_state_compact_noop(self):
        loop, _ = _make_loop()
        session = Session(key="test")
        session.add_message("user", "hi")
        ctx = TurnContext(msg=InboundMessage(content="hi", chat_id="test"), session_key="test")
        ctx.session = session
        event = await loop._state_compact(ctx)
        self.assertEqual(event, "ok")
        self.assertEqual(ctx.session.last_consolidated, 0)

    async def test_state_compact_with_summary(self):
        loop, _ = _make_loop(consolidator_provider=_MockProvider(), replay_budget=200)
        session = Session(key="test")
        for i in range(20):
            session.add_message("user", "x" * 200 + str(i))
        ctx = TurnContext(msg=InboundMessage(content="hi", chat_id="test"), session_key="test")
        ctx.session = session
        event = await loop._state_compact(ctx)
        self.assertEqual(event, "ok")
        self.assertIsNotNone(ctx.summary)
        self.assertGreater(session.last_consolidated, 0)

    async def test_state_build(self):
        loop, _ = _make_loop()
        ctx = TurnContext(msg=InboundMessage(content="hi", chat_id="test"), session_key="test")
        await loop._state_restore(ctx)
        ctx.session.add_message("user", "previous message")
        await loop._state_compact(ctx)
        event = await loop._state_build(ctx)
        self.assertEqual(event, "ok")
        self.assertEqual(len(ctx.history), 1)
        self.assertEqual(ctx.history[0]["content"], "previous message")
        self.assertGreater(len(ctx.initial_messages), 1)
        self.assertEqual(ctx.initial_messages[-1]["role"], "user")
        self.assertEqual(ctx.initial_messages[-1]["content"], "hi")

    async def test_state_run(self):
        loop, _ = _make_loop()
        session = Session(key="test")
        ctx = TurnContext(msg=InboundMessage(content="hello", chat_id="test"), session_key="test")
        ctx.session = session
        ctx.summary = "Summary: test"
        ctx.history = []
        ctx.initial_messages = loop.context.build_messages(
            current_message="hello", identity="You are a test bot.", session_summary="Summary: test",
        )
        event = await loop._state_run(ctx)
        self.assertEqual(event, "ok")
        self.assertIsNotNone(ctx.result)
        self.assertIn("Summary", ctx.result.final_content)

    async def test_state_save(self):
        loop, _ = _make_loop()
        session = Session(key="test")
        ctx = TurnContext(msg=InboundMessage(content="hello", chat_id="test"), session_key="test")
        ctx.session = session
        ctx.summary = "Summary: test"
        ctx.history = []
        ctx.initial_messages = loop.context.build_messages(
            current_message="hello", identity="You are a test bot.", session_summary="Summary: test",
        )
        await loop._state_run(ctx)
        event = await loop._state_save(ctx)
        self.assertEqual(event, "ok")
        self.assertGreater(len(session.messages), 0)
        self.assertEqual(session.messages[-1]["role"], "assistant")

    async def test_state_respond(self):
        loop, _ = _make_loop()
        session = Session(key="test")
        ctx = TurnContext(msg=InboundMessage(content="hello", chat_id="test"), session_key="test")
        ctx.session = session
        ctx.summary = "Summary: test"
        ctx.history = []
        ctx.initial_messages = loop.context.build_messages(
            current_message="hello", identity="You are a test bot.", session_summary="Summary: test",
        )
        await loop._state_run(ctx)
        await loop._state_save(ctx)
        event = await loop._state_respond(ctx)
        self.assertEqual(event, "ok")
        self.assertIsNotNone(ctx.outbound)
        self.assertIn("Summary", ctx.outbound.content)

    async def test_state_transitions(self):
        """Verify all states transition correctly through the table."""
        loop, _ = _make_loop()
        ctx = TurnContext(msg=InboundMessage(content="hi", chat_id="trans"), session_key="trans")

        for expected_state in [TurnState.COMPACT, TurnState.BUILD, TurnState.RUN,
                                TurnState.SAVE, TurnState.RESPOND, TurnState.DONE]:
            handler = getattr(loop, f"_state_{ctx.state.name.lower()}")
            event = await handler(ctx)
            ctx.state = loop._TRANSITIONS[(ctx.state, event)]
            self.assertEqual(ctx.state, expected_state)

    async def test_error_in_state_caught_by_process_message(self):
        loop, bus = _make_loop()

        class _CrashingProvider:
            @property
            def model(self):
                raise RuntimeError("provider error")

        loop.provider = _CrashingProvider()
        result = await loop._process_message(
            InboundMessage(content="hi", chat_id="crash"), "crash",
        )
        self.assertIsNotNone(result)
        self.assertIn("Error", result.content)
        self.assertEqual(result.metadata.get("stop_reason"), "error")


class TestAgentLoopIntegration(unittest.IsolatedAsyncioTestCase):
    """Integration tests that run the full AgentLoop."""

    async def test_full_turn(self):
        loop, bus = _make_loop()
        task = _run_loop(loop)
        await bus.publish_inbound(InboundMessage(content="hello", chat_id="test"))
        response = await bus.consume_outbound()
        self.assertIsNotNone(response.content)
        self.assertIn("stop_reason", response.metadata)
        loop.stop()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_full_turn_with_history(self):
        loop, bus = _make_loop(consolidator_provider=_MockProvider())
        task = _run_loop(loop)

        await bus.publish_inbound(InboundMessage(content="first", chat_id="multi"))
        r1 = await bus.consume_outbound()
        self.assertIsNotNone(r1.content)

        await bus.publish_inbound(InboundMessage(content="second", chat_id="multi"))
        r2 = await bus.consume_outbound()
        self.assertIsNotNone(r2.content)

        session = loop.sessions.get_or_create("multi")
        user_msgs = [m for m in session.messages if m["role"] == "user"]
        self.assertGreaterEqual(len(user_msgs), 2)

        loop.stop()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_per_session_lock(self):
        loop, bus = _make_loop()
        task = _run_loop(loop)
        results = []

        async def send_and_collect(key, text):
            await bus.publish_inbound(InboundMessage(content=text, chat_id=key))
            resp = await bus.consume_outbound()
            results.append(resp.content)

        t1 = asyncio.create_task(send_and_collect("lock_test", "msg1"))
        t2 = asyncio.create_task(send_and_collect("lock_test", "msg2"))
        await asyncio.gather(t1, t2)

        self.assertEqual(len(results), 2)
        session = loop.sessions.get_or_create("lock_test")
        self.assertEqual(len(session.messages), 4)

        loop.stop()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_cross_session_concurrent(self):
        loop, bus = _make_loop()
        task = _run_loop(loop)

        async def send_and_collect(key, text):
            await bus.publish_inbound(InboundMessage(content=text, chat_id=key))
            return await bus.consume_outbound()

        t1 = asyncio.create_task(send_and_collect("sess_a", "hello a"))
        t2 = asyncio.create_task(send_and_collect("sess_b", "hello b"))
        r1, r2 = await asyncio.gather(t1, t2)

        self.assertIsNotNone(r1.content)
        self.assertIsNotNone(r2.content)

        sess_a = loop.sessions.get_or_create("sess_a")
        sess_b = loop.sessions.get_or_create("sess_b")
        self.assertEqual(len(sess_a.messages), 2)
        self.assertEqual(len(sess_b.messages), 2)

        loop.stop()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_loop_stop_exits(self):
        loop, bus = _make_loop()
        task = _run_loop(loop)
        await asyncio.sleep(0.05)
        loop.stop()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_agent_roundtrip_via_loop(self):
        loop, bus = _make_loop()
        task = _run_loop(loop)
        await bus.publish_inbound(InboundMessage(content="hello"))
        response = await bus.consume_outbound()
        self.assertIsNotNone(response.content)
        self.assertIn("Summary", response.content)
        loop.stop()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task


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
            content="test", channel="discord", sender_id="user1",
            chat_id="chat1", session_key="custom", metadata={"key": "val"},
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


class TestTokenEstimator(unittest.TestCase):
    def test_text_message(self):
        tokens = estimate_message_tokens({"role": "user", "content": "hello"})
        self.assertGreaterEqual(tokens, 4)

    def test_long_text(self):
        tokens = estimate_message_tokens({"role": "user", "content": "a" * 100})
        self.assertGreaterEqual(tokens, 25)

    def test_with_tool_calls(self):
        msg = {
            "role": "assistant", "content": "",
            "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "echo", "arguments": '{"text":"hi"}'}}],
        }
        tokens = estimate_message_tokens(msg)
        self.assertGreater(tokens, 10)

    def test_with_tool_result(self):
        msg = {"role": "tool", "content": "Echo: hi", "name": "echo", "tool_call_id": "c1"}
        tokens = estimate_message_tokens(msg)
        self.assertGreaterEqual(tokens, 4)

    def test_estimate_prompt_tokens(self):
        msgs = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
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
