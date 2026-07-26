"""Tests for Step 11 — Hook system."""

import asyncio
import tempfile
import unittest
from typing import Any

from step11.bus import MessageBus
from step11.consolidation import Consolidator, estimate_message_tokens, estimate_prompt_tokens
from step11.context import ContextBuilder
from step11.events import InboundMessage, OutboundMessage
from step11.hook import AgentHook, AgentHookContext, AgentRunHookContext, CompositeHook
from step11.llm import LLMResponse, ToolCallRequest
from step11.loop import AgentLoop, TurnContext, TurnState
from step11.runner import AgentRunSpec, AgentRunner
from step11.session import Session, SessionManager
from step11.tool import ToolRegistry, Tool


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
            usage={"prompt_tokens": 50, "completion_tokens": 30},
        )


class _MockToolRegistry:
    def get_definitions(self):
        return []

    async def execute(self, name, **params):
        return ""


# ── Hook Tests ──

class _TrackingHook(AgentHook):
    """Records every hook invocation for verification."""

    def __init__(self):
        self.calls: list[str] = []
        self.before_run_ctx: AgentRunHookContext | None = None
        self.after_run_ctx: AgentRunHookContext | None = None
        self.on_error_ctx: AgentRunHookContext | None = None
        self.on_finally_ctx: AgentRunHookContext | None = None
        self.before_iter_ctxs: list[AgentHookContext] = []
        self.after_iter_ctxs: list[AgentHookContext] = []

    async def before_run(self, ctx):
        self.calls.append("before_run")
        self.before_run_ctx = ctx

    async def after_run(self, ctx):
        self.calls.append("after_run")
        self.after_run_ctx = ctx

    async def on_error(self, ctx):
        self.calls.append("on_error")
        self.on_error_ctx = ctx

    async def on_finally(self, ctx):
        self.calls.append("on_finally")
        self.on_finally_ctx = ctx

    async def before_iteration(self, ctx):
        self.calls.append("before_iteration")
        self.before_iter_ctxs.append(ctx)

    async def after_iteration(self, ctx):
        self.calls.append("after_iteration")
        self.after_iter_ctxs.append(ctx)


class _ErrorHook(AgentHook):
    """Raises in a specific method for isolation tests."""

    def __init__(self, fail_in: str = "before_iteration"):
        self.fail_in = fail_in

    async def before_iteration(self, ctx):
        if self.fail_in == "before_iteration":
            raise RuntimeError("hook error")

    async def after_iteration(self, ctx):
        if self.fail_in == "after_iteration":
            raise RuntimeError("hook error")


class TestHookLifecycle(unittest.IsolatedAsyncioTestCase):
    async def test_before_run_called(self):
        hook = _TrackingHook()
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "hi"}],
            tools=_MockToolRegistry(),
            provider=_MockProvider(),
            hook=hook,
        )
        await AgentRunner().run(spec)
        self.assertIn("before_run", hook.calls)

    async def test_after_run_called_on_success(self):
        hook = _TrackingHook()
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "hi"}],
            tools=_MockToolRegistry(),
            provider=_MockProvider(),
            hook=hook,
        )
        await AgentRunner().run(spec)
        self.assertIn("after_run", hook.calls)
        self.assertIsNotNone(hook.after_run_ctx)
        self.assertIsNotNone(hook.after_run_ctx.final_content)

    async def test_on_error_called_on_exception(self):
        hook = _TrackingHook()

        class _FailProvider:
            @property
            def model(self):
                return "mock"
            async def chat_with_retry(self, **kwargs):
                raise RuntimeError("provider failure")

        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "hi"}],
            tools=_MockToolRegistry(),
            provider=_FailProvider(),
            hook=hook,
        )
        with self.assertRaises(RuntimeError):
            await AgentRunner().run(spec)
        self.assertIn("on_error", hook.calls)
        self.assertIsNotNone(hook.on_error_ctx)
        self.assertIsInstance(hook.on_error_ctx.exception, RuntimeError)

    async def test_on_finally_always_called(self):
        hook = _TrackingHook()

        class _FailProvider:
            @property
            def model(self):
                return "mock"
            async def chat_with_retry(self, **kwargs):
                raise RuntimeError("fail")

        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "hi"}],
            tools=_MockToolRegistry(),
            provider=_FailProvider(),
            hook=hook,
        )
        with self.assertRaises(RuntimeError):
            await AgentRunner().run(spec)
        self.assertIn("on_finally", hook.calls)
        self.assertIsNotNone(hook.on_finally_ctx)

        # on_finally also called on success
        hook2 = _TrackingHook()
        spec2 = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "hi"}],
            tools=_MockToolRegistry(),
            provider=_MockProvider(),
            hook=hook2,
        )
        await AgentRunner().run(spec2)
        self.assertIn("on_finally", hook2.calls)

    async def test_before_iteration_called(self):
        hook = _TrackingHook()
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "hi"}],
            tools=_MockToolRegistry(),
            provider=_MockProvider(),
            hook=hook,
        )
        await AgentRunner().run(spec)
        self.assertIn("before_iteration", hook.calls)
        self.assertEqual(len(hook.before_iter_ctxs), 1)

    async def test_after_iteration_called(self):
        hook = _TrackingHook()
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "hi"}],
            tools=_MockToolRegistry(),
            provider=_MockProvider(),
            hook=hook,
        )
        await AgentRunner().run(spec)
        self.assertIn("after_iteration", hook.calls)
        self.assertEqual(len(hook.after_iter_ctxs), 1)

    async def test_iteration_context_state(self):
        hook = _TrackingHook()
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "hi"}],
            tools=_MockToolRegistry(),
            provider=_MockProvider(),
            hook=hook,
        )
        await AgentRunner().run(spec)
        ctx = hook.after_iter_ctxs[0]
        self.assertEqual(ctx.iteration, 0)
        self.assertGreaterEqual(len(ctx.messages), 1)
        self.assertIsNotNone(ctx.response)
        self.assertIsNotNone(ctx.final_content)
        self.assertIn("Summary", ctx.final_content)
        self.assertIn("prompt_tokens", ctx.usage)

    async def test_run_context_state(self):
        hook = _TrackingHook()
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "hi"}],
            tools=_MockToolRegistry(),
            provider=_MockProvider(),
            hook=hook,
        )
        await AgentRunner().run(spec)
        ctx = hook.after_run_ctx
        self.assertIsNotNone(ctx)
        self.assertIsNotNone(ctx.final_content)
        self.assertEqual(ctx.stop_reason, "stop")
        self.assertIn("Summary", ctx.final_content)

    async def test_composite_hook_fanout(self):
        h1 = _TrackingHook()
        h2 = _TrackingHook()
        composite = CompositeHook([h1, h2])
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "hi"}],
            tools=_MockToolRegistry(),
            provider=_MockProvider(),
            hook=composite,
        )
        await AgentRunner().run(spec)
        for h in (h1, h2):
            self.assertIn("before_run", h.calls)
            self.assertIn("after_run", h.calls)
            self.assertIn("before_iteration", h.calls)
            self.assertIn("after_iteration", h.calls)
            self.assertIn("on_finally", h.calls)

    async def test_hook_error_isolation(self):
        tracking = _TrackingHook()
        error_hook = _ErrorHook(fail_in="before_iteration")
        composite = CompositeHook([tracking, error_hook])
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "hi"}],
            tools=_MockToolRegistry(),
            provider=_MockProvider(),
            hook=composite,
        )
        # Should not raise — CompositeHook isolates errors
        result = await AgentRunner().run(spec)
        self.assertIsNotNone(result)
        self.assertIsNotNone(result.final_content)
        # tracking hook should still have been called despite error_hook failing
        self.assertIn("before_iteration", tracking.calls)

    async def test_custom_hook_usage_tracker(self):
        class UsageTracker(AgentHook):
            def __init__(self):
                self.total_prompt = 0
                self.total_completion = 0

            async def after_iteration(self, ctx):
                self.total_prompt += ctx.usage.get("prompt_tokens", 0)
                self.total_completion += ctx.usage.get("completion_tokens", 0)

        hook = UsageTracker()
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "hi"}],
            tools=_MockToolRegistry(),
            provider=_MockProvider(),
            hook=hook,
        )
        await AgentRunner().run(spec)
        self.assertGreater(hook.total_prompt, 0)
        self.assertGreater(hook.total_completion, 0)

    async def test_session_key_in_context(self):
        hook = _TrackingHook()
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "hi"}],
            tools=_MockToolRegistry(),
            provider=_MockProvider(),
            hook=hook,
            session_key="test_sess",
        )
        await AgentRunner().run(spec)
        for ctx in hook.before_iter_ctxs:
            self.assertEqual(ctx.session_key, "test_sess")


class TestHookWithToolIterations(unittest.IsolatedAsyncioTestCase):
    async def test_multiple_iterations_with_tools(self):
        class _EchoToolRegistry:
            def __init__(self):
                self.executed = []

            def get_definitions(self):
                return [{"type": "function", "function": {"name": "echo", "description": "echo", "parameters": {"type": "object", "properties": {"text": {"type": "string"}}}}}]  # noqa: E501

            async def execute(self, name, **params):
                self.executed.append((name, params))
                return ToolResult(f"Echo: {params.get('text', '')}")

        class _ToolCallProvider:
            @property
            def model(self):
                return "mock"
            async def chat_with_retry(self, **kwargs):
                # Return tool_calls for first call, then text
                is_first = kwargs.get("messages", [{}])[-1].get("role") == "user"
                if is_first:
                    return LLMResponse(
                        content="",
                        tool_calls=[ToolCallRequest(id="call_1", name="echo", arguments={"text": "hello"})],
                        finish_reason="tool_calls",
                        usage={"prompt_tokens": 50, "completion_tokens": 10},
                    )
                return LLMResponse(
                    content="Done after tool call.",
                    finish_reason="stop",
                    usage={"prompt_tokens": 60, "completion_tokens": 5},
                )

        hook = _TrackingHook()
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "hi"}],
            tools=_EchoToolRegistry(),
            provider=_ToolCallProvider(),
            hook=hook,
            max_iterations=3,
        )
        result = await AgentRunner().run(spec)
        self.assertIsNotNone(result)
        self.assertGreater(len(hook.before_iter_ctxs), 1)
        # Each iteration should have tool_calls populated
        for ctx in hook.before_iter_ctxs:
            self.assertIsNotNone(ctx)


from step11.tool import ToolResult


class TestAgentLoopWithHook(unittest.IsolatedAsyncioTestCase):
    def _make_loop(self, hooks=None):
        bus = MessageBus()
        provider = _MockProvider()
        registry = _MockToolRegistry()
        tmp = tempfile.mkdtemp()
        session_manager = SessionManager(workspace=tmp)
        context_builder = ContextBuilder(workspace=".")
        consolidator = Consolidator()
        loop = AgentLoop(
            bus=bus, provider=provider, registry=registry,
            session_manager=session_manager, context_builder=context_builder,
            consolidator=consolidator, identity="You are a test bot.",
            replay_budget=10000, hooks=hooks,
        )
        return loop, bus

    async def test_loop_with_hook(self):
        hook = _TrackingHook()
        loop, bus = self._make_loop(hooks=[hook])
        task = asyncio.create_task(loop.run())
        await bus.publish_inbound(InboundMessage(content="hello"))
        response = await bus.consume_outbound()
        self.assertIsNotNone(response.content)
        self.assertIn("before_run", hook.calls)
        self.assertIn("after_run", hook.calls)
        self.assertIn("before_iteration", hook.calls)
        self.assertIn("after_iteration", hook.calls)
        self.assertIn("on_finally", hook.calls)
        loop.stop()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task


# ── Existing AgentLoop Tests ──

class TestAgentLoopStateHandlers(unittest.IsolatedAsyncioTestCase):
    async def test_state_restore(self):
        loop, _ = self._make_loop()
        ctx = TurnContext(msg=InboundMessage(content="hi", chat_id="test"), session_key="test")
        event = await loop._state_restore(ctx)
        self.assertEqual(event, "ok")
        self.assertIsNotNone(ctx.session)
        self.assertEqual(ctx.session.key, "test")

    def _make_loop(self):
        bus = MessageBus()
        provider = _MockProvider()
        registry = _MockToolRegistry()
        tmp = tempfile.mkdtemp()
        session_manager = SessionManager(workspace=tmp)
        context_builder = ContextBuilder(workspace=".")
        consolidator = Consolidator()
        return AgentLoop(
            bus=bus, provider=provider, registry=registry,
            session_manager=session_manager, context_builder=context_builder,
            consolidator=consolidator, identity="You are a test bot.",
            replay_budget=10000,
        ), bus

    async def test_state_compact_noop(self):
        loop, _ = self._make_loop()
        session = Session(key="test")
        session.add_message("user", "hi")
        ctx = TurnContext(msg=InboundMessage(content="hi", chat_id="test"), session_key="test")
        ctx.session = session
        event = await loop._state_compact(ctx)
        self.assertEqual(event, "ok")
        self.assertEqual(ctx.session.last_consolidated, 0)

    async def test_state_compact_with_summary(self):
        loop, _ = self._make_loop()
        loop.consolidator = Consolidator(provider=_MockProvider())
        loop.replay_budget = 200
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
        loop, _ = self._make_loop()
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
        loop, _ = self._make_loop()
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
        loop, _ = self._make_loop()
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
        loop, _ = self._make_loop()
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
        loop, _ = self._make_loop()
        ctx = TurnContext(msg=InboundMessage(content="hi", chat_id="trans"), session_key="trans")
        for expected_state in [TurnState.COMPACT, TurnState.BUILD, TurnState.RUN,
                                TurnState.SAVE, TurnState.RESPOND, TurnState.DONE]:
            handler = getattr(loop, f"_state_{ctx.state.name.lower()}")
            event = await handler(ctx)
            ctx.state = loop._TRANSITIONS[(ctx.state, event)]
            self.assertEqual(ctx.state, expected_state)

    async def test_error_in_state_caught_by_process_message(self):
        loop, _ = self._make_loop()

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
    def _make_loop(self):
        bus = MessageBus()
        provider = _MockProvider()
        registry = _MockToolRegistry()
        tmp = tempfile.mkdtemp()
        session_manager = SessionManager(workspace=tmp)
        context_builder = ContextBuilder(workspace=".")
        consolidator = Consolidator()
        loop = AgentLoop(
            bus=bus, provider=provider, registry=registry,
            session_manager=session_manager, context_builder=context_builder,
            consolidator=consolidator, identity="You are a test bot.",
            replay_budget=10000,
        )
        return loop, bus

    async def test_full_turn(self):
        loop, bus = self._make_loop()
        task = asyncio.create_task(loop.run())
        await bus.publish_inbound(InboundMessage(content="hello", chat_id="test"))
        response = await bus.consume_outbound()
        self.assertIsNotNone(response.content)
        loop.stop()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_per_session_lock(self):
        loop, bus = self._make_loop()
        task = asyncio.create_task(loop.run())
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
        loop, bus = self._make_loop()
        task = asyncio.create_task(loop.run())

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
        loop, bus = self._make_loop()
        task = asyncio.create_task(loop.run())
        await asyncio.sleep(0.05)
        loop.stop()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task


# ── Existing MessageBus Tests ──

class TestMessageBus(unittest.IsolatedAsyncioTestCase):
    async def test_publish_consume_inbound(self):
        bus = MessageBus()
        msg = InboundMessage(content="hello")
        await bus.publish_inbound(msg)
        received = await bus.consume_inbound()
        self.assertEqual(received.content, "hello")

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


# ── Existing Token Estimator Tests ──

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

    def test_max_tokens_returns_all_if_under_budget(self):
        session = Session(key="test")
        session.add_message("user", "hi")
        history = session.get_history(max_messages=50, max_tokens=10000)
        self.assertEqual(len(history), 1)

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


class TestContextBuilderWithSummary(unittest.TestCase):
    def test_session_summary_in_system_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = ContextBuilder(workspace=tmp)
            prompt = ctx.build_system_prompt(session_summary="User likes Python.")
        self.assertIn("Archived Context Summary", prompt)

    def test_no_summary_when_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = ContextBuilder(workspace=tmp)
            prompt = ctx.build_system_prompt()
        self.assertNotIn("Archived Context Summary", prompt)


if __name__ == "__main__":
    unittest.main()
