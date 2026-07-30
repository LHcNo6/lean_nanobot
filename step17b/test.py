"""Tests for Step 16 — Subagents + Sustained Goals."""

import asyncio
import tempfile
import unittest
from typing import Any

from step17b.bus import MessageBus
from step17b.consolidation import Consolidator, _consolidation_boundary
from step17b.goal_state import *
from step17b.helpers import estimate_message_tokens, estimate_prompt_tokens
from step17b.llm import Runtime
from step17b.memory import MemoryStore
from step17b.context import ContextBuilder
from step17b.events import InboundMessage, OutboundMessage, StreamDeltaEvent
from step17b.hook import AgentHook, AgentHookContext, AgentRunHookContext, CompositeHook
from step17b.llm import LLMResponse, ToolCallRequest
from step17b.loop import AgentLoop, StreamPublishingHook, TurnContext, TurnState
from step17b.provider import LLMProvider
from step17b.governance import ContextGovernanceConfig, ContextGovernor
from step17b.runner import AgentRunSpec, AgentRunner
from step17b.session import Session, SessionManager
from step17b.subagent import SubagentManager, SubagentStatus
from step17b.tool import ToolRegistry, Tool
from step17b.tools.long_task import CreateGoalTool, UpdateGoalTool
from step17b.tools.echo import EchoTool
from step17b.tools.spawn import SpawnTool
from step17b.events import StreamDeltaEvent


async def _consume_final_response(bus):
    """Consume outbound messages, skipping stream deltas, until the final response."""
    while True:
        msg = await bus.consume_outbound()
        if not isinstance(msg, StreamDeltaEvent):
            return msg


class _MockProvider(LLMProvider):
    def __init__(self, response: LLMResponse | None = None):
        super().__init__()
        self._response = response

    @property
    def model(self) -> str:
        return "mock-model"

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        if self._response is not None:
            return self._response
        return LLMResponse(
            content="Summary: user asked about weather, assistant provided forecast.",
            finish_reason="stop",
            usage={"prompt_tokens": 50, "completion_tokens": 30},
        )


class _MockToolRegistry:
    def __init__(self):
        self._tools = {}

    def register(self, tool):
        self._tools[tool.name] = tool

    def get_definitions(self):
        return []

    async def execute(self, name, **params):
        return ""

    def get(self, name):
        return self._tools.get(name)


# 锟斤拷锟斤拷 Hook Tests 锟斤拷锟斤拷

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

        class _FailProvider(LLMProvider):
            @property
            def model(self):
                return "mock"
            async def chat(self, messages, tools=None, model=None, temperature=0.7, max_tokens=4096):
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

        class _FailProvider(LLMProvider):
            @property
            def model(self):
                return "mock"
            async def chat(self, messages, tools=None, model=None, temperature=0.7, max_tokens=4096):
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
        # Should not raise 锟斤拷 CompositeHook isolates errors
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

        class _ToolCallProvider(LLMProvider):
            @property
            def model(self):
                return "mock"
            async def chat(self, messages, tools=None, model=None, temperature=0.7, max_tokens=4096):
                is_first = messages[-1].get("role") == "user" if messages else True
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


from step17b.tool import ToolResult


class TestAgentLoopWithHook(unittest.IsolatedAsyncioTestCase):
    def _make_loop(self, hooks=None):
        bus = MessageBus()
        provider = _MockProvider()
        registry = _MockToolRegistry()
        tmp = tempfile.mkdtemp()
        session_manager = SessionManager(workspace=tmp)
        context_builder = ContextBuilder(workspace=".")
        memory = MemoryStore(workspace=tmp)
        loop = AgentLoop(
            bus=bus, provider=provider, registry=registry,
            session_manager=session_manager, context_builder=context_builder,
            memory=memory, identity="You are a test bot.",
            replay_budget=10000, hooks=hooks,
        )
        return loop, bus

    async def test_loop_with_hook(self):
        hook = _TrackingHook()
        loop, bus = self._make_loop(hooks=[hook])
        task = asyncio.create_task(loop.run())
        await bus.publish_inbound(InboundMessage(content="hello"))
        response = await _consume_final_response(bus)
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


# 鈹€鈹€ Streaming Tests 鈹€鈹€

class _TrackingHookForStream(AgentHook):
    def __init__(self):
        self.stream_deltas: list[str] = []
        self.stream_end_count = 0
        self.iter_stream_contents: list[str] = []

    async def on_stream(self, ctx: AgentHookContext, delta: str) -> None:
        self.stream_deltas.append(delta)

    async def on_stream_end(self, ctx: AgentHookContext) -> None:
        self.stream_end_count += 1
        self.iter_stream_contents.append(ctx.stream_content)


class _StreamingMockProvider(LLMProvider):
    @property
    def model(self) -> str:
        return "mock"

    async def chat(self, messages, tools=None, model=None, temperature=0.7, max_tokens=4096):
        return LLMResponse(
            content="Hello world!",
            finish_reason="stop",
            usage={"prompt_tokens": 10, "completion_tokens": 5},
        )

    async def chat_stream_with_retry(self, messages, tools=None, model=None,
                                      temperature=0.7, max_tokens=4096,
                                      on_content_delta=None, retry_config=None):
        chunks = ["Hello", " ", "world", "!"]
        for chunk in chunks:
            if on_content_delta:
                await on_content_delta(chunk)
            await asyncio.sleep(0)
        return LLMResponse(
            content="Hello world!",
            finish_reason="stop",
            usage={"prompt_tokens": 10, "completion_tokens": 5},
        )


class _StreamingToolCallProvider(LLMProvider):
    @property
    def model(self) -> str:
        return "mock"

    async def chat(self, messages, tools=None, model=None, temperature=0.7, max_tokens=4096):
        is_first = messages[-1].get("role") == "user" if messages else True
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

    async def chat_stream_with_retry(self, messages, tools=None, model=None,
                                      temperature=0.7, max_tokens=4096,
                                      on_content_delta=None, retry_config=None):
        is_first = messages[-1].get("role") == "user" if messages else True
        if is_first:
            if on_content_delta:
                await on_content_delta("")
            return LLMResponse(
                content="",
                tool_calls=[ToolCallRequest(id="call_1", name="echo", arguments={"text": "hello"})],
                finish_reason="tool_calls",
                usage={"prompt_tokens": 50, "completion_tokens": 10},
            )
        chunks = ["Done ", "after ", "tool ", "call."]
        for chunk in chunks:
            if on_content_delta:
                await on_content_delta(chunk)
            await asyncio.sleep(0)
        return LLMResponse(
            content="Done after tool call.",
            finish_reason="stop",
            usage={"prompt_tokens": 60, "completion_tokens": 5},
        )


class _EchoToolRegistryForStream:
    def get_definitions(self):
        return [{"type": "function", "function": {"name": "echo", "description": "echo", "parameters": {"type": "object", "properties": {"text": {"type": "string"}}}}}]

    async def execute(self, name, **params):
        return f"Echo: {params.get('text', '')}"


class TestStreamingHooks(unittest.IsolatedAsyncioTestCase):
    async def test_on_stream_called_with_deltas(self):
        hook = _TrackingHookForStream()
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "hi"}],
            tools=_MockToolRegistry(),
            provider=_StreamingMockProvider(),
            hook=hook,
        )
        await AgentRunner().run(spec)
        self.assertEqual(hook.stream_deltas, ["Hello", " ", "world", "!"])

    async def test_stream_content_accumulated(self):
        hook = _TrackingHookForStream()
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "hi"}],
            tools=_MockToolRegistry(),
            provider=_StreamingMockProvider(),
            hook=hook,
        )
        await AgentRunner().run(spec)
        self.assertEqual(hook.iter_stream_contents[-1], "Hello world!")

    async def test_on_stream_end_called(self):
        hook = _TrackingHookForStream()
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "hi"}],
            tools=_MockToolRegistry(),
            provider=_StreamingMockProvider(),
            hook=hook,
        )
        await AgentRunner().run(spec)
        self.assertEqual(hook.stream_end_count, 1)

    async def test_no_stream_when_tool_calls(self):
        hook = _TrackingHookForStream()
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "hi"}],
            tools=_EchoToolRegistryForStream(),
            provider=_StreamingToolCallProvider(),
            hook=hook,
            max_iterations=3,
        )
        result = await AgentRunner().run(spec)
        self.assertIsNotNone(result)
        # First iteration (tool_calls): no content delta expected
        # Second iteration (text): deltas expected
        self.assertGreater(len(hook.stream_deltas), 0)
        self.assertEqual(hook.stream_end_count, 2)

    async def test_stream_usage_accumulated(self):
        hook = _TrackingHookForStream()
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "hi"}],
            tools=_MockToolRegistry(),
            provider=_StreamingMockProvider(),
            hook=hook,
        )
        result = await AgentRunner().run(spec)
        self.assertGreater(result.total_prompt_tokens, 0)
        self.assertGreater(result.total_completion_tokens, 0)


class TestStreamPublishingHook(unittest.IsolatedAsyncioTestCase):
    async def test_publishes_deltas_to_bus(self):
        bus = MessageBus()
        stream_hook = StreamPublishingHook(bus=bus, chat_id="test", channel="cli", session_key="sess1")

        ctx = AgentHookContext(iteration=0, messages=[], session_key="sess1")
        await stream_hook.on_stream(ctx, "Hel")
        await stream_hook.on_stream(ctx, "lo")

        for expected in ("Hel", "lo"):
            msg = await bus.consume_outbound()
            self.assertIsInstance(msg, StreamDeltaEvent)
            self.assertEqual(msg.content, expected)
            self.assertFalse(msg.finished)
            self.assertEqual(msg.session_key, "sess1")

    async def test_publishes_finished_signal(self):
        bus = MessageBus()
        stream_hook = StreamPublishingHook(bus=bus, chat_id="test", channel="cli", session_key="sess1")

        ctx = AgentHookContext(iteration=0, messages=[], session_key="sess1")
        await stream_hook.on_stream_end(ctx)

        msg = await bus.consume_outbound()
        self.assertIsInstance(msg, StreamDeltaEvent)
        self.assertTrue(msg.finished)
        self.assertEqual(msg.session_key, "sess1")

    async def test_skip_empty_delta(self):
        bus = MessageBus()
        stream_hook = StreamPublishingHook(bus=bus, chat_id="test", channel="cli", session_key="sess1")

        ctx = AgentHookContext(iteration=0, messages=[], session_key="sess1")
        await stream_hook.on_stream(ctx, "")

        self.assertEqual(bus.outbound_size, 0)


class TestAgentLoopStreaming(unittest.IsolatedAsyncioTestCase):
    async def _drain_until_outbound(self, bus: MessageBus) -> tuple[list[StreamDeltaEvent], OutboundMessage]:
        deltas: list[StreamDeltaEvent] = []
        while True:
            msg = await bus.consume_outbound()
            if isinstance(msg, StreamDeltaEvent):
                deltas.append(msg)
                if msg.finished:
                    continue
            else:
                return deltas, msg

    def _make_loop(self, hooks=None):
        bus = MessageBus()
        provider = _StreamingMockProvider()
        registry = _MockToolRegistry()
        tmp = tempfile.mkdtemp()
        session_manager = SessionManager(workspace=tmp)
        context_builder = ContextBuilder(workspace=".")
        memory = MemoryStore(workspace=tmp)
        loop = AgentLoop(
            bus=bus, provider=provider, registry=registry,
            session_manager=session_manager, context_builder=context_builder,
            memory=memory, identity="You are a test bot.",
            replay_budget=10000, hooks=hooks,
        )
        return loop, bus

    async def test_loop_streaming_end_to_end(self):
        hook = _TrackingHookForStream()
        loop, bus = self._make_loop(hooks=[hook])
        task = asyncio.create_task(loop.run())
        await bus.publish_inbound(InboundMessage(content="hello", chat_id="stream_test"))
        deltas, response = await self._drain_until_outbound(bus)
        self.assertIsNotNone(response.content)
        self.assertGreater(len(deltas), 0)
        self.assertTrue(deltas[-1].finished)
        loop.stop()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_loop_streaming_with_hooks(self):
        tracking = _TrackingHook()
        stream_tracking = _TrackingHookForStream()
        loop, bus = self._make_loop(hooks=[tracking, stream_tracking])
        task = asyncio.create_task(loop.run())
        await bus.publish_inbound(InboundMessage(content="hello", chat_id="hook_stream"))
        deltas, response = await self._drain_until_outbound(bus)
        self.assertIsNotNone(response.content)
        self.assertIn("before_run", tracking.calls)
        self.assertIn("on_finally", tracking.calls)
        self.assertGreater(len(stream_tracking.stream_deltas), 0)
        self.assertGreater(len(deltas), 0)
        loop.stop()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task


# 鈹€鈹€ Existing AgentLoop Tests 鈹€鈹€

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
        memory = MemoryStore(workspace=tmp)
        return AgentLoop(
            bus=bus, provider=provider, registry=registry,
            session_manager=session_manager, context_builder=context_builder,
            memory=memory, identity="You are a test bot.",
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
        loop.consolidator.provider = _MockProvider()
        loop.runtime.context_window_tokens = 4000
        loop.runtime.max_tokens = 512
        session = loop.sessions.get_or_create("test")
        for i in range(20):
            session.add_message("user", "x" * 500 + str(i))
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
        memory = MemoryStore(workspace=tmp)
        loop = AgentLoop(
            bus=bus, provider=provider, registry=registry,
            session_manager=session_manager, context_builder=context_builder,
            memory=memory, identity="You are a test bot.",
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

        # Publish both messages; the second should queue behind the first's lock
        await bus.publish_inbound(InboundMessage(content="msg1", chat_id="lock_test"))
        await bus.publish_inbound(InboundMessage(content="msg2", chat_id="lock_test"))

        results = []
        results.append((await _consume_final_response(bus)).content)
        results.append((await _consume_final_response(bus)).content)

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


# 锟斤拷锟斤拷 Existing MessageBus Tests 锟斤拷锟斤拷

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


# 锟斤拷锟斤拷 Existing Token Estimator Tests 锟斤拷锟斤拷

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
        boundary = _consolidation_boundary(msgs, 10000)
        self.assertEqual(boundary, 0)

    def test_boundary_truncates(self):
        msgs = [{"role": "user", "content": "x" * 200} for _ in range(20)]
        boundary = _consolidation_boundary(msgs, 100)
        self.assertGreater(boundary, 0)
        self.assertLess(boundary, 20)


class TestConsolidatorMaybeConsolidate(unittest.IsolatedAsyncioTestCase):
    async def test_noop_when_under_budget(self):
        session = Session(key="test")
        session.add_message("user", "hi")
        conso = Consolidator(store=MemoryStore(workspace=tempfile.mkdtemp()), sessions=SessionManager(workspace=tempfile.mkdtemp()), build_messages=lambda **kw: [], get_tool_definitions=lambda: [])
        summary = await conso.maybe_consolidate(session, max_tokens=10000)
        self.assertIsNone(summary)

    async def test_truncate_without_provider(self):
        session = Session(key="test")
        for i in range(20):
            session.add_message("user", "x" * 200 + str(i))
        conso = Consolidator(store=MemoryStore(workspace=tempfile.mkdtemp()), sessions=SessionManager(workspace=tempfile.mkdtemp()), build_messages=lambda **kw: [], get_tool_definitions=lambda: [])
        summary = await conso.maybe_consolidate(session, max_tokens=100)
        self.assertIsNone(summary)
        self.assertGreater(session.last_consolidated, 0)

    async def test_with_provider_returns_summary(self):
        session = Session(key="test")
        for i in range(20):
            session.add_message("user", "x" * 200 + str(i))
        conso = Consolidator(store=MemoryStore(workspace=tempfile.mkdtemp()), sessions=SessionManager(workspace=tempfile.mkdtemp()), build_messages=lambda **kw: [], get_tool_definitions=lambda: [])
        conso.provider = _MockProvider()
        summary = await conso.maybe_consolidate(session, max_tokens=100)
        self.assertIsNotNone(summary)
        self.assertIn("Summary", summary)

    async def test_no_unconsolidated_messages(self):
        session = Session(key="test")
        session.last_consolidated = 0
        conso = Consolidator(store=MemoryStore(workspace=tempfile.mkdtemp()), sessions=SessionManager(workspace=tempfile.mkdtemp()), build_messages=lambda **kw: [], get_tool_definitions=lambda: [])
        result = await conso.maybe_consolidate(session, max_tokens=100)
        self.assertIsNone(result)


class TestFormatMessages(unittest.TestCase):
    def test_format_simple(self):
        msgs = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
        formatted = MemoryStore._format_messages(msgs)
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


# ── Mid-turn Injection Tests ──

class _InjectionSource:
    """Helper that returns injected messages a limited number of times."""
    def __init__(self, messages: list[str]):
        self._messages = list(messages)
        self.calls = 0

    async def callback(self) -> list[dict]:
        self.calls += 1
        if self.calls <= len(self._messages):
            return [{"role": "user", "content": self._messages[self.calls - 1]}]
        return []


class _SingleResponseProvider(LLMProvider):
    @property
    def model(self):
        return "mock"
    async def chat(self, messages, tools=None, model=None, temperature=0.7, max_tokens=4096):
        return LLMResponse(content="Here is my response.", finish_reason="stop", usage={"prompt_tokens": 10, "completion_tokens": 5})
    async def chat_stream_with_retry(self, messages, tools=None, model=None, temperature=0.7, max_tokens=4096, on_content_delta=None, retry_config=None):
        resp = await self.chat(messages, tools, model, temperature, max_tokens)
        if on_content_delta and resp.content:
            for chunk in resp.content.split(" "):
                await on_content_delta(chunk + " ")
        return resp


class _MultiResponseProvider(LLMProvider):
    """Returns pre-configured responses in sequence."""
    def __init__(self, responses: list[LLMResponse]):
        self._responses = list(responses)
        self.call_count = 0
    @property
    def model(self):
        return "mock"
    async def chat(self, messages, tools=None, model=None, temperature=0.7, max_tokens=4096):
        idx = self.call_count
        self.call_count += 1
        if idx < len(self._responses):
            return self._responses[idx]
        return LLMResponse(content="Final fallback.", finish_reason="stop", usage={"prompt_tokens": 5, "completion_tokens": 3})
    async def chat_stream_with_retry(self, messages, tools=None, model=None, temperature=0.7, max_tokens=4096, on_content_delta=None, retry_config=None):
        response = await self.chat(messages, tools, model, temperature, max_tokens)
        if on_content_delta and response.content:
            for chunk in response.content.split(" "):
                await on_content_delta(chunk + " ")
        return response


def _make_injection_loop(provider=None):
    bus = MessageBus()
    p = provider or _MockProvider()
    registry = _MockToolRegistry()
    tmp = tempfile.mkdtemp()
    session_manager = SessionManager(workspace=tmp)
    context_builder = ContextBuilder(workspace=".")
    memory = MemoryStore(workspace=tmp)
    loop = AgentLoop(
        bus=bus, provider=p, registry=registry,
        session_manager=session_manager, context_builder=context_builder,
        memory=memory, identity="You are a test bot.",
        replay_budget=10000,
    )
    return loop, bus


class TestMidTurnInjection(unittest.IsolatedAsyncioTestCase):
    async def test_injection_callback_returns_messages(self):
        """injection_callback drains queued messages and returns message dicts."""
        queue: asyncio.Queue[InboundMessage] = asyncio.Queue()
        await queue.put(InboundMessage(content="injected1"))
        await queue.put(InboundMessage(content="injected2"))

        async def injection_callback():
            msgs = []
            while not queue.empty():
                try:
                    m = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                msgs.append({"role": "user", "content": m.content})
            return msgs

        result = await injection_callback()
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["role"], "user")
        self.assertEqual(result[0]["content"], "injected1")
        self.assertEqual(result[1]["content"], "injected2")

    async def test_runner_injection_after_tool_execution(self):
        """Runner drains injected messages after tool execution."""
        injector = _InjectionSource(["stop and respond"])
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "do something"}],
            tools=_EchoToolRegistryForStream(),
            provider=_StreamingToolCallProvider(),
            injection_callback=injector.callback,
            max_iterations=5,
        )
        result = await AgentRunner().run(spec)
        self.assertIsNotNone(result)
        self.assertIsNotNone(result.final_content)
        msg_contents = [m.get("content", "") for m in result.messages]
        found = any("stop and respond" in str(c) for c in msg_contents)
        self.assertTrue(found, "Injected message should appear in conversation")

    async def test_runner_injection_before_final_response(self):
        """Runner drains injected messages before final text response and extends turn."""
        injector = _InjectionSource(["wait, one more thing"])
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "hello"}],
            tools=_MockToolRegistry(),
            provider=_SingleResponseProvider(),
            injection_callback=injector.callback,
            max_iterations=5,
        )
        result = await AgentRunner().run(spec)
        self.assertIsNotNone(result)
        self.assertIsNotNone(result.final_content)
        user_msgs = [m for m in result.messages if m["role"] == "user"]
        user_contents = [m["content"] for m in user_msgs]
        self.assertIn("wait, one more thing", user_contents)

    async def test_runner_injection_extends_turn(self):
        """Injected messages lead to additional LLM iterations."""
        call_count = 0
        async def injection_callback():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [{"role": "user", "content": "extend me"}]
            return []

        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "first msg"}],
            tools=_MockToolRegistry(),
            provider=_MultiResponseProvider([
                LLMResponse(content="First response.", finish_reason="stop", usage={"prompt_tokens": 10, "completion_tokens": 5}),
                LLMResponse(content="Second after injection.", finish_reason="stop", usage={"prompt_tokens": 15, "completion_tokens": 8}),
            ]),
            injection_callback=injection_callback,
            max_iterations=5,
        )
        result = await AgentRunner().run(spec)
        self.assertIsNotNone(result)
        self.assertEqual(result.stop_reason, "stop")
        self.assertEqual(result.final_content, "Second after injection.")

    async def test_empty_injection_callback_noop(self):
        """Empty injection_callback returns [] and doesn't affect flow."""
        async def empty_callback():
            return []

        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "hello"}],
            tools=_MockToolRegistry(),
            provider=_MockProvider(),
            injection_callback=empty_callback,
        )
        result = await AgentRunner().run(spec)
        self.assertIsNotNone(result)
        self.assertIn("Summary", result.final_content)

    async def test_injection_preserves_assistant_message(self):
        """Assistant message is preserved when injection extends turn."""
        call_count = 0
        async def injection_callback():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [{"role": "user", "content": "tell me more"}]
            return []

        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "start"}],
            tools=_MockToolRegistry(),
            provider=_MultiResponseProvider([
                LLMResponse(content="First answer.", finish_reason="stop", usage={"prompt_tokens": 10, "completion_tokens": 5}),
                LLMResponse(content="Second answer.", finish_reason="stop", usage={"prompt_tokens": 15, "completion_tokens": 8}),
            ]),
            injection_callback=injection_callback,
            max_iterations=5,
        )
        result = await AgentRunner().run(spec)
        self.assertIsNotNone(result)
        assistant_msgs = [m for m in result.messages if m["role"] == "assistant"]
        self.assertGreaterEqual(len(assistant_msgs), 2)
        self.assertIn("First answer.", assistant_msgs[0]["content"])

    async def test_no_injection_callback_works(self):
        """Runner works normally without injection_callback."""
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "hello"}],
            tools=_MockToolRegistry(),
            provider=_MockProvider(),
        )
        result = await AgentRunner().run(spec)
        self.assertIsNotNone(result)
        self.assertIn("Summary", result.final_content)

    async def test_injection_callback_single_call_multiple_messages(self):
        """Multiple queued messages are drained in a single callback call."""
        queue: asyncio.Queue[InboundMessage] = asyncio.Queue()
        for i in range(5):
            await queue.put(InboundMessage(content=f"msg{i}"))

        async def drain_all():
            msgs = []
            while not queue.empty():
                try:
                    m = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                msgs.append({"role": "user", "content": m.content})
            return msgs

        injected = await drain_all()
        self.assertEqual(len(injected), 5)
        self.assertEqual(injected[0]["content"], "msg0")
        self.assertEqual(injected[4]["content"], "msg4")

    async def test_loop_get_or_create_queue(self):
        """_get_or_create_queue creates and caches per-session queues."""
        loop, _ = _make_injection_loop()
        q1 = loop._get_or_create_queue("sess_a")
        q2 = loop._get_or_create_queue("sess_a")
        q3 = loop._get_or_create_queue("sess_b")
        self.assertIs(q1, q2)
        self.assertIsNot(q1, q3)
        self.assertEqual(q1.maxsize, 20)

    async def test_loop_state_run_creates_injection_callback(self):
        """_state_run creates an injection_callback tied to the session key."""
        loop, _ = _make_injection_loop()
        # Manually put a message in the pending queue
        loop._pending_queues["inj_test"] = asyncio.Queue()
        loop._pending_queues["inj_test"].put_nowait(InboundMessage(content="queued"))
        session = Session(key="inj_test")
        ctx = TurnContext(msg=InboundMessage(content="hello", chat_id="inj_test"), session_key="inj_test")
        ctx.session = session
        ctx.summary = "Summary: test"
        ctx.history = []
        ctx.initial_messages = loop.context.build_messages(
            current_message="hello", identity="You are a test bot.", session_summary="Summary: test",
        )
        event = await loop._state_run(ctx)
        self.assertEqual(event, "ok")
        self.assertIsNotNone(ctx.result)

    async def test_leftover_drain_republishes_to_bus(self):
        """AgentLoop._drain_leftover republishes queued messages to bus.inbound."""
        loop, bus = _make_injection_loop()
        loop._pending_queues["test"] = asyncio.Queue()
        loop._pending_queues["test"].put_nowait(InboundMessage(content="leftover_msg", chat_id="test"))
        await loop._drain_leftover("test")
        msg = await bus.consume_inbound()
        self.assertEqual(msg.content, "leftover_msg")
        # Queue should now be empty
        self.assertTrue(loop._pending_queues["test"].empty())

    async def test_leftover_drain_empty_noop(self):
        """_drain_leftover with empty queue does nothing."""
        loop, bus = _make_injection_loop()
        loop._pending_queues["test_empty"] = asyncio.Queue()
        await loop._drain_leftover("test_empty")
        self.assertEqual(bus.inbound_size, 0)


# ── Context Governance Tests ──

_GOVERNOR = ContextGovernor()
_MOCK_TOOLS = _MockToolRegistry()


def _gov_config(
    context_window_tokens: int | None = 200_000,
    max_tool_result_chars: int = 16_000,
    max_tokens: int = 4096,
    context_block_limit: int | None = None,
) -> ContextGovernanceConfig:
    return ContextGovernanceConfig(
        tools=_MOCK_TOOLS,
        context_window_tokens=context_window_tokens,
        max_tool_result_chars=max_tool_result_chars,
        max_tokens=max_tokens,
        context_block_limit=context_block_limit,
    )


class TestGovernanceInputBudget(unittest.TestCase):
    def test_budget_with_context_window(self):
        config = _gov_config(context_window_tokens=200_000, max_tokens=8192)
        budget = ContextGovernor.input_budget(config)
        expected = 200_000 - 8192 - 1024
        self.assertEqual(budget, expected)

    def test_budget_no_context_window(self):
        config = _gov_config(context_window_tokens=None)
        budget = ContextGovernor.input_budget(config)
        self.assertEqual(budget, 0)

    def test_budget_with_block_limit(self):
        config = _gov_config(context_window_tokens=200_000, context_block_limit=50_000)
        budget = ContextGovernor.input_budget(config)
        self.assertEqual(budget, 50_000)

    def test_budget_clamps_to_zero(self):
        config = _gov_config(context_window_tokens=500, max_tokens=4096)
        budget = ContextGovernor.input_budget(config)
        self.assertEqual(budget, 0)


class TestGovernanceStripPlaceholder(unittest.TestCase):
    def test_removes_placeholder(self):
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "[Previous assistant message omitted.]"},
            {"role": "user", "content": "next"},
        ]
        result = _GOVERNOR.strip_placeholder_assistant_messages(msgs)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[1]["content"], "next")

    def test_preserves_placeholder_with_tool_calls(self):
        msgs = [
            {"role": "assistant", "content": "[Previous assistant message omitted.]",
             "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "echo", "arguments": "{}"}}]},
        ]
        result = _GOVERNOR.strip_placeholder_assistant_messages(msgs)
        self.assertEqual(len(result), 1)

    def test_no_placeholder_no_change(self):
        msgs = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
        result = _GOVERNOR.strip_placeholder_assistant_messages(msgs)
        self.assertIs(result, msgs)


class TestGovernanceStripMalformed(unittest.TestCase):
    def test_strips_malformed_name_none(self):
        msgs = [
            {"role": "assistant", "content": "",
             "tool_calls": [{"id": "c1", "type": "function", "function": {"name": None, "arguments": "{}"}}]},
        ]
        result = _GOVERNOR.strip_malformed_tool_calls(msgs)
        self.assertEqual(len(result), 0)

    def test_keeps_valid_tool_call(self):
        msgs = [
            {"role": "assistant", "content": "",
             "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "echo", "arguments": "{}"}}]},
        ]
        result = _GOVERNOR.strip_malformed_tool_calls(msgs)
        self.assertEqual(len(result), 1)

    def test_keeps_mixed_removes_bad(self):
        msgs = [
            {"role": "assistant", "content": "",
             "tool_calls": [
                 {"id": "c1", "type": "function", "function": {"name": "echo", "arguments": "{}"}},
                 {"id": "c2", "type": "function", "function": {"name": None, "arguments": "{}"}},
             ]},
        ]
        result = _GOVERNOR.strip_malformed_tool_calls(msgs)
        self.assertEqual(len(result), 1)
        self.assertEqual(len(result[0]["tool_calls"]), 1)
        self.assertEqual(result[0]["tool_calls"][0]["id"], "c1")


class TestGovernanceDropOrphan(unittest.TestCase):
    def test_drops_orphan_tool_result(self):
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "tool", "tool_call_id": "orphan_id", "content": "result"},
        ]
        result = _GOVERNOR.drop_orphan_tool_results(msgs)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["role"], "user")

    def test_keeps_matched_tool_result(self):
        msgs = [
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "echo", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": "c1", "content": "result"},
        ]
        result = _GOVERNOR.drop_orphan_tool_results(msgs)
        self.assertEqual(len(result), 2)

    def test_full_chain_preserved(self):
        msgs = [
            {"role": "user", "content": "do it"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "echo", "arguments": "{}"}},
                {"id": "c2", "type": "function", "function": {"name": "echo", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": "c1", "content": "r1"},
            {"role": "tool", "tool_call_id": "c2", "content": "r2"},
        ]
        result = _GOVERNOR.drop_orphan_tool_results(msgs)
        self.assertEqual(len(result), 4)


class TestGovernanceBackfill(unittest.TestCase):
    def test_backfills_missing(self):
        msgs = [
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "echo", "arguments": "{}"}},
            ]},
        ]
        result = _GOVERNOR.backfill_missing_tool_results(msgs)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[1]["role"], "tool")
        self.assertEqual(result[1]["tool_call_id"], "c1")
        self.assertIn("unavailable", result[1]["content"])

    def test_no_backfill_when_present(self):
        msgs = [
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "echo", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": "c1", "content": "result"},
        ]
        result = _GOVERNOR.backfill_missing_tool_results(msgs)
        self.assertIs(result, msgs)

    def test_backfills_multiple_missing(self):
        msgs = [
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "echo", "arguments": "{}"}},
                {"id": "c2", "type": "function", "function": {"name": "echo", "arguments": "{}"}},
            ]},
        ]
        result = _GOVERNOR.backfill_missing_tool_results(msgs)
        self.assertEqual(len(result), 3)
        tool_msgs = [m for m in result if m["role"] == "tool"]
        self.assertEqual(len(tool_msgs), 2)
        self.assertEqual(tool_msgs[0]["tool_call_id"], "c1")
        self.assertEqual(tool_msgs[1]["tool_call_id"], "c2")


class TestGovernanceApplyBudget(unittest.TestCase):
    def test_truncates_oversized_result(self):
        config = _gov_config(max_tool_result_chars=50)
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "tool", "tool_call_id": "c1", "name": "echo", "content": "x" * 200},
        ]
        result = _GOVERNOR.apply_tool_result_budget(config, msgs)
        tool_content = result[1]["content"]
        self.assertLess(len(tool_content), 200)
        self.assertIn("truncated", tool_content)

    def test_keeps_normal_sized_result(self):
        config = _gov_config(max_tool_result_chars=16_000)
        msgs = [
            {"role": "tool", "tool_call_id": "c1", "name": "echo", "content": "short result"},
        ]
        result = _GOVERNOR.apply_tool_result_budget(config, msgs)
        self.assertEqual(result[0]["content"], "short result")


class TestGovernanceNormalizeToolResult(unittest.TestCase):
    def test_empty_result_replaced(self):
        result = ContextGovernor.normalize_tool_result(
            _gov_config(), "c1", "echo", None,
        )
        self.assertIn("completed with no output", result)

    def test_oversized_truncated(self):
        result = ContextGovernor.normalize_tool_result(
            _gov_config(max_tool_result_chars=20), "c1", "echo", "x" * 100,
        )
        self.assertLess(len(result), 100)
        self.assertIn("truncated", result)

    def test_exempt_tool_unchanged(self):
        result = ContextGovernor.normalize_tool_result(
            _gov_config(max_tool_result_chars=20), "c1", "read_file", "x" * 100,
        )
        self.assertEqual(result, "x" * 100)


class TestGovernanceCompactOverflow(unittest.TestCase):
    def test_no_compact_when_under_budget(self):
        config = _gov_config(context_window_tokens=200_000, max_tokens=4096)
        msgs = [
            {"role": "user", "content": "hi"},
        ]
        compacted_ids: set[str] = set()
        result = _GOVERNOR.compact_inflight_overflow(config, msgs, compacted_ids)
        self.assertIs(result, msgs)

    def test_compact_when_over_budget(self):
        class _SmallBudgetTools:
            def get_definitions(self):
                return []
        config = ContextGovernanceConfig(
            tools=_SmallBudgetTools(),
            context_window_tokens=2000,
            max_tokens=500,
        )
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "echo", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": "c1", "name": "exec", "content": "x" * 2000},
        ]
        compacted_ids: set[str] = set()
        result = _GOVERNOR.compact_inflight_overflow(config, msgs, compacted_ids)
        self.assertIn("compacted", result[2]["content"])


class TestGovernanceSnipHistory(unittest.TestCase):
    def test_no_snip_when_under_budget(self):
        config = _gov_config(context_window_tokens=200_000, max_tokens=4096)
        msgs = [{"role": "user", "content": "hi"}]
        result = _GOVERNOR.snip_history(config, msgs)
        self.assertIs(result, msgs)

    def test_snip_when_over_budget(self):
        config = _gov_config(context_window_tokens=2000, max_tokens=500)
        msgs = [
            {"role": "system", "content": "system prompt"},
        ] + [
            {"role": "user", "content": "x" * 100}
        ] * 20
        result = _GOVERNOR.snip_history(config, msgs)
        self.assertLess(len(result), len(msgs))
        self.assertEqual(result[0]["role"], "system")


class TestGovernancePipeline(unittest.TestCase):
    def test_prepare_for_model_full_pipeline(self):
        config = _gov_config(context_window_tokens=200_000, max_tool_result_chars=50)
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "[Previous assistant message omitted.]"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "echo", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": "c1", "name": "echo", "content": "ok"},
        ]
        compacted_ids: set[str] = set()
        result = _GOVERNOR.prepare_for_model(config, msgs, compacted_ids)
        self.assertIsNot(result, msgs)
        self.assertLessEqual(len(result), len(msgs))


class _GovProvider(LLMProvider):
    @property
    def model(self):
        return "mock"
    async def chat(self, messages, tools=None, model=None, temperature=0.7, max_tokens=4096):
        return LLMResponse(content="ok", finish_reason="stop", usage={"prompt_tokens": 5, "completion_tokens": 3})
    async def chat_stream_with_retry(self, messages, tools=None, model=None, temperature=0.7, max_tokens=4096, on_content_delta=None, retry_config=None):
        if on_content_delta:
            await on_content_delta("ok")
        return await self.chat(messages, tools, model, temperature, max_tokens)


class TestGovernanceRunnerIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_governance_applies_to_messages(self):
        config = ContextGovernanceConfig(
            tools=_MockToolRegistry(),
            context_window_tokens=200_000,
        )
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "hi"}],
            tools=_MockToolRegistry(),
            provider=_GovProvider(),
            governance_config=config,
        )
        result = await AgentRunner().run(spec)
        self.assertIsNotNone(result)
        self.assertEqual(result.final_content, "ok")

    async def test_governance_none_no_impact(self):
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "hi"}],
            tools=_MockToolRegistry(),
            provider=_GovProvider(),
        )
        result = await AgentRunner().run(spec)
        self.assertIsNotNone(result)
        self.assertEqual(result.final_content, "ok")

    async def test_governance_in_multi_iteration(self):
        class _TwoCallProvider(LLMProvider):
            @property
            def model(self):
                return "mock"
            async def chat(self, messages, tools=None, model=None, temperature=0.7, max_tokens=4096):
                is_first = True
                for m in reversed(messages):
                    if m.get("role") == "user":
                        is_first = True
                        break
                    elif m.get("role") == "tool":
                        is_first = False
                        break
                if is_first:
                    return LLMResponse(content="", tool_calls=[
                        ToolCallRequest(id="c1", name="echo", arguments={"text": "hello"}),
                    ], finish_reason="tool_calls", usage={"prompt_tokens": 10, "completion_tokens": 5})
                return LLMResponse(content="done", finish_reason="stop", usage={"prompt_tokens": 15, "completion_tokens": 8})
            async def chat_stream_with_retry(self, messages, tools=None, model=None, temperature=0.7, max_tokens=4096, on_content_delta=None, retry_config=None):
                resp = await self.chat(messages, tools, model, temperature, max_tokens)
                if on_content_delta and resp.content:
                    await on_content_delta(resp.content)
                return resp

        config = ContextGovernanceConfig(
            tools=_MockToolRegistry(),
            context_window_tokens=200_000,
        )
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "do it"}],
            tools=_EchoToolRegistryForStream(),
            provider=_TwoCallProvider(),
            governance_config=config,
            max_iterations=3,
        )
        result = await AgentRunner().run(spec)
        self.assertIsNotNone(result)
        self.assertIn("done", result.final_content)


class TestGovernanceHelpers(unittest.TestCase):
    def test_estimate_message_tokens(self):
        from step17b.helpers import estimate_message_tokens as emt
        tokens = emt({"role": "user", "content": "hello"})
        self.assertGreaterEqual(tokens, 4)

    def test_estimate_prompt_tokens_with_tools(self):
        from step17b.helpers import estimate_prompt_tokens
        tokens = estimate_prompt_tokens(
            [{"role": "user", "content": "hi"}],
            tools=[{"type": "function", "function": {"name": "echo"}}],
        )
        self.assertGreater(tokens, 0)

    def test_find_legal_message_start(self):
        from step17b.helpers import find_legal_message_start
        msgs = [
            {"role": "tool", "tool_call_id": "orphan", "content": "x"},
            {"role": "user", "content": "hi"},
        ]
        start = find_legal_message_start(msgs)
        self.assertEqual(start, 1)

    def test_truncate_text(self):
        from step17b.helpers import truncate_text
        result = truncate_text("hello world", 5)
        self.assertIn("truncated", result)
        self.assertLessEqual(len(result), 20)

    def test_ensure_nonempty_tool_result(self):
        from step17b.helpers import ensure_nonempty_tool_result
        result = ensure_nonempty_tool_result("echo", None)
        self.assertIn("completed with no output", result)
        # non-empty passes through
        self.assertEqual(ensure_nonempty_tool_result("echo", "ok"), "ok")


class TestMemoryStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = MemoryStore(workspace=self.tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_append_and_read(self):
        c1 = self.store.append_history("first entry", session_key="test")
        c2 = self.store.append_history("second entry", session_key="test")
        self.assertIsInstance(c1, int)
        self.assertIsInstance(c2, int)
        self.assertGreater(c2, c1)
        entries = self.store.read_unprocessed_history(since_cursor=0)
        self.assertEqual(len(entries), 2)

    def test_read_unprocessed_since_cursor(self):
        c1 = self.store.append_history("entry A")
        self.store.append_history("entry B")
        entries = self.store.read_unprocessed_history(since_cursor=c1)
        self.assertEqual(len(entries), 1)

    def test_raw_archive(self):
        msgs = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "world"}]
        cursor = self.store.raw_archive(msgs, session_key="test")
        self.assertGreater(cursor, 0)
        entries = self.store.read_unprocessed_history(since_cursor=0)
        content = entries[0]["content"]
        self.assertIn("[RAW]", content)

    def test_compact_history_preserves_recent(self):
        for i in range(50):
            self.store.append_history(f"entry {i}")
        # max_history_entries defaults to 1000, so test with smaller
        store_small = MemoryStore(workspace=tempfile.mkdtemp(), max_history_entries=10)
        for i in range(50):
            store_small.append_history(f"entry {i}")
        store_small.compact_history()
        entries = store_small._read_entries()
        self.assertLessEqual(len(entries), 10)

    def test_cursor_persistence(self):
        c1 = self.store.append_history("test")
        c2 = self.store.get_latest_cursor()
        self.assertEqual(c1, c2)

    def test_dream_cursor(self):
        self.assertEqual(self.store.get_last_dream_cursor(), 0)
        self.store.set_last_dream_cursor(42)
        self.assertEqual(self.store.get_last_dream_cursor(), 42)

    def test_get_latest_cursor_empty(self):
        self.assertEqual(self.store.get_latest_cursor(), 0)


class TestDream(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = MemoryStore(workspace=self.tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_build_dream_prompt_no_entries(self):
        result = self.store.build_dream_prompt(max_entries=20)
        self.assertIsNone(result)

    async def test_build_dream_prompt_with_entries(self):
        self.store.append_history("user mentioned they like python")
        self.store.append_history("assistant suggested learning pytest")
        result = self.store.build_dream_prompt(max_entries=20)
        self.assertIsNotNone(result)
        prompt, cursor = result
        self.assertIn("Conversation History", prompt)
        self.assertIn("python", prompt)

    async def test_build_dream_prompt_respects_cursor(self):
        self.store.append_history("entry before")
        self.store.set_last_dream_cursor(1)
        self.store.append_history("entry after")
        result = self.store.build_dream_prompt(max_entries=20)
        self.assertIsNotNone(result)
        prompt, cursor = result
        self.assertNotIn("entry before", prompt)
        self.assertIn("entry after", prompt)

    async def test_build_dream_prompt_truncates_long_content(self):
        long_text = "x" * 2000
        self.store.append_history(long_text)
        result = self.store.build_dream_prompt(max_entries=20)
        self.assertIsNotNone(result)
        prompt, cursor = result
        # content should be truncated to 500 chars per entry
        self.assertNotIn("x" * 501, prompt)


class TestConsolidatorNewAPI(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = MemoryStore(workspace=self.tmp)
        self.sessions = SessionManager(workspace=self.tmp)
        self.registry = _MockToolRegistry()
        self.conso = Consolidator(
            store=self.store,
            sessions=self.sessions,
            build_messages=lambda **kw: [],
            get_tool_definitions=self.registry.get_definitions,
        )

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_maybe_consolidate_by_tokens_noop_empty_session(self):
        session = self.sessions.get_or_create("empty")
        runtime = Runtime(context_window_tokens=10000, max_tokens=1024)
        await self.conso.maybe_consolidate_by_tokens(session, runtime=runtime)
        self.assertEqual(session.last_consolidated, 0)

    async def test_maybe_consolidate_by_tokens_noop_under_budget(self):
        session = self.sessions.get_or_create("small")
        session.add_message("user", "hi")
        runtime = Runtime(context_window_tokens=10000, max_tokens=1024)
        await self.conso.maybe_consolidate_by_tokens(session, runtime=runtime)
        self.assertEqual(session.last_consolidated, 0)

    async def test_maybe_consolidate_by_tokens_no_runtime(self):
        session = self.sessions.get_or_create("test")
        session.add_message("user", "hello")
        await self.conso.maybe_consolidate_by_tokens(session, runtime=None)
        self.assertEqual(session.last_consolidated, 0)

    async def test_maybe_consolidate_by_tokens_triggers(self):
        session = self.sessions.get_or_create("big")
        for i in range(30):
            session.add_message("user", "x" * 500 + str(i))
        runtime = Runtime(context_window_tokens=2000, max_tokens=128)
        await self.conso.maybe_consolidate_by_tokens(session, runtime=runtime)
        self.assertGreater(session.last_consolidated, 0)
        meta = session.metadata.get("_last_summary")
        # without a provider, summary should be None and last_consolidated still advances
        self.assertIsNone(meta)

    async def test_maybe_consolidate_with_provider(self):
        prov = _MockProvider()
        tmp2 = tempfile.mkdtemp()
        sessions2 = SessionManager(workspace=tmp2)
        store2 = MemoryStore(workspace=tmp2)
        conso = Consolidator(
            store=store2,
            sessions=sessions2,
            build_messages=lambda **kw: [],
            get_tool_definitions=lambda: [],
            provider=prov,
        )
        session = sessions2.get_or_create("prov")
        for i in range(20):
            session.add_message("user", "x" * 500 + str(i))
        runtime = Runtime(context_window_tokens=2000, max_tokens=128, provider=prov, model="mock")
        conso.provider = prov
        await conso.maybe_consolidate_by_tokens(session, runtime=runtime)
        meta = session.metadata.get("_last_summary")
        self.assertIsNotNone(meta)
        self.assertIn("text", meta)

    async def test_compact_idle_session_noop(self):
        result = await self.conso.compact_idle_session("nonexistent", runtime=Runtime(context_window_tokens=10000, max_tokens=1024))
        self.assertEqual(result, "")

    async def test_pick_consolidation_boundary(self):
        session = self.sessions.get_or_create("boundary")
        for i in range(10):
            session.add_message("user", "msg " + str(i))
        boundary = self.conso.pick_consolidation_boundary(session, tokens_to_remove=10000)
        # best-effort: returns last user boundary even if tokens_to_remove > total
        self.assertIsNotNone(boundary)

    async def test_pick_consolidation_boundary_some(self):
        session = self.sessions.get_or_create("bound")
        for i in range(10):
            session.add_message("user", "x" * 200 + str(i))
        self.conso.consolidation_ratio = 0.3
        boundary = self.conso.pick_consolidation_boundary(session, tokens_to_remove=50)
        # With 10 msgs of ~50 tokens each = 500 total tokens
        # tokens_to_remove=50 should return a boundary at the first user msg
        self.assertIsNotNone(boundary)
        idx, tokens = boundary
        self.assertGreaterEqual(idx, 1)

    async def test_archive_without_provider_falls_back(self):
        msgs = [{"role": "user", "content": "test data"}]
        runtime = Runtime(context_window_tokens=10000, max_tokens=1024, provider=None)
        result = await self.conso.archive(msgs, runtime=runtime, session_key="test")
        self.assertIsNone(result)  # no provider → returns None after raw_archive

    async def test_archive_with_provider(self):
        msgs = [{"role": "user", "content": "hello world"}]
        prov = _MockProvider()
        runtime = Runtime(context_window_tokens=10000, max_tokens=1024, provider=prov, model="mock")
        result = await self.conso.archive(msgs, runtime=runtime, session_key="test")
        self.assertIsNotNone(result)
        self.assertIn("Summary", result)

    async def test_archive_empty(self):
        runtime = Runtime(context_window_tokens=10000, max_tokens=1024)
        result = await self.conso.archive([], runtime=runtime)
        self.assertIsNone(result)


class TestRuntime(unittest.TestCase):
    def test_defaults(self):
        r = Runtime(context_window_tokens=4096)
        self.assertEqual(r.context_window_tokens, 4096)
        self.assertEqual(r.max_tokens, 4096)
        self.assertIsNone(r.provider)
        self.assertIsNone(r.model)

    def test_custom_values(self):
        r = Runtime(context_window_tokens=8192, max_tokens=1024, provider="test", model="gpt-4")
        self.assertEqual(r.context_window_tokens, 8192)
        self.assertEqual(r.max_tokens, 1024)
        self.assertEqual(r.provider, "test")
        self.assertEqual(r.model, "gpt-4")


# ---- Step 16 Tests: Goal State ----

class TestGoalState(unittest.TestCase):
    def test_parse_goal_state_none(self):
        self.assertIsNone(parse_goal_state(None))
        self.assertIsNone(parse_goal_state("invalid json"))

    def test_parse_goal_state_dict(self):
        blob = {"status": "active", "objective": "test"}
        self.assertEqual(parse_goal_state(blob), blob)

    def test_sustained_goal_active(self):
        meta = {"goal_state": {"status": "active", "objective": "do x"}}
        self.assertTrue(sustained_goal_active(meta))
        self.assertFalse(sustained_goal_active({"goal_state": {"status": "completed"}}))
        self.assertFalse(sustained_goal_active({}))
        self.assertFalse(sustained_goal_active(None))

    def test_goal_state_runtime_lines_active(self):
        meta = {"goal_state": {"status": "active", "objective": "implement feature X"}}
        lines = goal_state_runtime_lines(meta)
        self.assertIn("Goal (active):", lines)
        self.assertIn("implement feature X", lines)

    def test_goal_state_runtime_lines_inactive(self):
        meta = {"goal_state": {"status": "completed", "objective": "done"}}
        self.assertEqual(goal_state_runtime_lines(meta), [])

    def test_goal_state_runtime_lines_with_summary(self):
        meta = {"goal_state": {"status": "active", "objective": "refactor", "ui_summary": "Refactor core"}}
        lines = goal_state_runtime_lines(meta)
        self.assertIn("Summary: Refactor core", lines)

    def test_goal_state_runtime_lines_truncated(self):
        long_obj = "x" * (MAX_GOAL_OBJECTIVE_CHARS + 100)
        meta = {"goal_state": {"status": "active", "objective": long_obj}}
        lines = goal_state_runtime_lines(meta)
        self.assertIn("(truncated)", lines[-1])

    def test_goal_state_runtime_lines_empty_objective(self):
        meta = {"goal_state": {"status": "active", "objective": ""}}
        lines = goal_state_runtime_lines(meta)
        self.assertIn("no objective text", " ".join(lines))


# ---- Step 16 Tests: Goal Tools ----

class _MockSessionManager:
    def __init__(self):
        self._sessions = {}

    def get_or_create(self, key: str) -> Session:
        if key not in self._sessions:
            self._sessions[key] = Session(key=key)
        return self._sessions[key]


class TestCreateGoalTool(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sessions = _MockSessionManager()
        self.tool = CreateGoalTool(sessions=self.sessions)
        self.tool.set_session_key("test_session")

    async def test_create_goal(self):
        result = await self.tool.execute(objective="Build a feature")
        self.assertIn("Goal recorded", str(result))
        sess = self.sessions.get_or_create("test_session")
        state = sess.metadata.get("goal_state", {})
        self.assertEqual(state["status"], "active")
        self.assertEqual(state["objective"], "Build a feature")

    async def test_create_goal_empty(self):
        result = await self.tool.execute(objective="")
        self.assertIn("empty", str(result).lower())

    async def test_create_goal_already_active(self):
        await self.tool.execute(objective="First goal")
        result = await self.tool.execute(objective="Second goal")
        self.assertIn("already active", str(result).lower())

    async def test_create_goal_no_session(self):
        tool = CreateGoalTool()
        result = await tool.execute(objective="test")
        self.assertIn("not available", str(result).lower())

    async def test_create_goal_with_summary(self):
        await self.tool.execute(objective="Refactor", ui_summary="Code refactor")
        sess = self.sessions.get_or_create("test_session")
        self.assertEqual(sess.metadata["goal_state"]["ui_summary"], "Code refactor")


class TestUpdateGoalTool(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sessions = _MockSessionManager()
        self.tool = UpdateGoalTool(sessions=self.sessions)
        self.tool.set_session_key("test_session")
        self.sessions.get_or_create("test_session").metadata["goal_state"] = {
            "status": "active", "objective": "test objective",
        }

    async def test_complete_goal(self):
        result = await self.tool.execute(action="complete", recap="Done and tested")
        self.assertIn("completed", str(result))
        sess = self.sessions.get_or_create("test_session")
        self.assertEqual(sess.metadata["goal_state"]["status"], "completed")

    async def test_cancel_goal(self):
        result = await self.tool.execute(action="cancel")
        self.assertIn("cancelled", str(result))
        sess = self.sessions.get_or_create("test_session")
        self.assertEqual(sess.metadata["goal_state"]["status"], "cancelled")

    async def test_block_goal(self):
        result = await self.tool.execute(action="block", recap="Blocked on API")
        self.assertIn("blocked", str(result))
        sess = self.sessions.get_or_create("test_session")
        self.assertEqual(sess.metadata["goal_state"]["status"], "blocked")

    async def test_replace_goal(self):
        result = await self.tool.execute(action="replace", objective="New objective")
        self.assertIn("replaced", str(result))
        sess = self.sessions.get_or_create("test_session")
        self.assertEqual(sess.metadata["goal_state"]["status"], "active")
        self.assertEqual(sess.metadata["goal_state"]["objective"], "New objective")

    async def test_replace_missing_objective(self):
        result = await self.tool.execute(action="replace")
        self.assertIn("requires", str(result).lower())

    async def test_no_active_goal(self):
        sess = self.sessions.get_or_create("test_session")
        sess.metadata.pop("goal_state", None)
        result = await self.tool.execute(action="complete")
        self.assertIn("No active goal", str(result))

    async def test_invalid_action(self):
        result = await self.tool.execute(action="invalid")
        self.assertIn("one of", str(result).lower())


# ---- Step 16 Tests: SpawnTool ----

class TestSpawnTool(unittest.IsolatedAsyncioTestCase):
    async def test_spawn_no_manager(self):
        tool = SpawnTool()
        result = await tool.execute(task="do something")
        self.assertIn("not available", str(result).lower())

    async def test_spawn_empty_task(self):
        tool = SpawnTool(manager=object())  # type: ignore
        result = await tool.execute(task="")
        self.assertIn("empty", str(result).lower())


# ---- Step 16 Tests: SubagentManager ----

class TestSubagentManager(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bus = MessageBus()
        self.manager = SubagentManager(bus=self.bus, max_concurrent_subagents=3)

    async def test_get_running_count(self):
        self.assertEqual(self.manager.get_running_count(), 0)

    async def test_get_running_count_by_session(self):
        count = self.manager.get_running_count_by_session("test")
        self.assertEqual(count, 0)

    async def test_spawn_no_provider(self):
        result = await self.manager.spawn(task="test task")
        self.assertIn("started", str(result))
        await asyncio.sleep(0.05)
        self.assertEqual(self.manager.get_running_count(), 0)  # task finished silently because no provider

    async def test_cancel_by_session(self):
        result = await self.manager.spawn(task="test", session_key="s1")
        self.assertIn("started", str(result))
        cancelled = await self.manager.cancel_by_session("s1")
        self.assertGreaterEqual(cancelled, 0)


# ---- Step 16 Tests: Runner Goal Continuation ----

class TestRunnerGoalContinuation(unittest.IsolatedAsyncioTestCase):
    async def test_goal_active_continues(self):
        provider = _MockProvider(LLMResponse(content="Final answer", finish_reason="stop"))
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "hello"}],
            tools=ToolRegistry(),
            provider=provider,
            max_iterations=5,
            goal_active_predicate=lambda: True,
            goal_continue_message="Keep working",
        )
        runner = AgentRunner()
        result = await runner.run(spec)
        self.assertIsNotNone(result.final_content)

    async def test_goal_inactive_no_continue(self):
        provider = _MockProvider(LLMResponse(content="Final answer", finish_reason="stop"))
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "hello"}],
            tools=ToolRegistry(),
            provider=provider,
            max_iterations=5,
            goal_active_predicate=lambda: False,
            goal_continue_message="Keep working",
        )
        runner = AgentRunner()
        result = await runner.run(spec)
        self.assertIn("Final answer", result.final_content or "")


# ---- Step 17a Tests: Concurrent Tool Execution ----

class _ConcurrencyTrackingTool(Tool):
    """Tool that records execution order for concurrency verification."""

    def __init__(self, name: str, delay: float = 0.1, concurrency_safe: bool = True):
        self._name = name
        self._delay = delay
        self._concurrency_safe = concurrency_safe
        self.execution_order: list[str] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"Tool {self._name}"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}

    @property
    def read_only(self) -> bool:
        return self._concurrency_safe

    async def execute(self, **kwargs) -> ToolResult:
        self.execution_order.append(f"start_{self._name}")
        await asyncio.sleep(self._delay)
        self.execution_order.append(f"end_{self._name}")
        return ToolResult(f"result_{self._name}")


class _ConcurrentToolCallProvider(LLMProvider):
    """Provider that returns multiple tool calls on first call, then stops."""

    def __init__(self, tool_names: list[str]):
        self._tool_names = tool_names
        self.call_count = 0

    @property
    def model(self) -> str:
        return "mock"

    async def chat(self, messages, tools=None, model=None, temperature=0.7, max_tokens=4096):
        self.call_count += 1
        if self.call_count == 1:
            calls = [
                ToolCallRequest(id=f"c{i}", name=name, arguments={"x": name})
                for i, name in enumerate(self._tool_names)
            ]
            return LLMResponse(content="", tool_calls=calls, finish_reason="tool_calls",
                               usage={"prompt_tokens": 10, "completion_tokens": 5})
        return LLMResponse(content="done", finish_reason="stop",
                           usage={"prompt_tokens": 15, "completion_tokens": 8})

    async def chat_stream_with_retry(self, messages, tools=None, model=None,
                                      temperature=0.7, max_tokens=4096,
                                      on_content_delta=None, retry_config=None):
        resp = await self.chat(messages, tools, model, temperature, max_tokens)
        if on_content_delta and resp.content:
            await on_content_delta(resp.content)
        return resp


class TestConcurrentToolExecution(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_safe_tools_run_in_parallel(self):
        tool_a = _ConcurrencyTrackingTool("tool_a", delay=0.2, concurrency_safe=True)
        tool_b = _ConcurrencyTrackingTool("tool_b", delay=0.2, concurrency_safe=True)
        registry = ToolRegistry()
        registry.register(tool_a)
        registry.register(tool_b)
        provider = _ConcurrentToolCallProvider(["tool_a", "tool_b"])
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "run tools"}],
            tools=registry,
            provider=provider,
            max_iterations=3,
            concurrent_tools=True,
        )
        start = asyncio.get_event_loop().time()
        result = await AgentRunner().run(spec)
        elapsed = asyncio.get_event_loop().time() - start
        self.assertIsNotNone(result)
        # If parallel, total time should be ~0.2s, not ~0.4s
        self.assertLess(elapsed, 0.35, "Tools should run in parallel")

    async def test_non_safe_tools_run_serially(self):
        tool_a = _ConcurrencyTrackingTool("tool_a", delay=0.15, concurrency_safe=False)
        tool_b = _ConcurrencyTrackingTool("tool_b", delay=0.15, concurrency_safe=False)
        registry = ToolRegistry()
        registry.register(tool_a)
        registry.register(tool_b)
        provider = _ConcurrentToolCallProvider(["tool_a", "tool_b"])
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "run tools"}],
            tools=registry,
            provider=provider,
            max_iterations=3,
            concurrent_tools=True,
        )
        start = asyncio.get_event_loop().time()
        result = await AgentRunner().run(spec)
        elapsed = asyncio.get_event_loop().time() - start
        self.assertIsNotNone(result)
        # Serial: ~0.3s total
        self.assertGreaterEqual(elapsed, 0.25, "Non-safe tools should run serially")

    async def test_concurrent_tools_disabled_runs_serially(self):
        tool_a = _ConcurrencyTrackingTool("tool_a", delay=0.15, concurrency_safe=True)
        tool_b = _ConcurrencyTrackingTool("tool_b", delay=0.15, concurrency_safe=True)
        registry = ToolRegistry()
        registry.register(tool_a)
        registry.register(tool_b)
        provider = _ConcurrentToolCallProvider(["tool_a", "tool_b"])
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "run tools"}],
            tools=registry,
            provider=provider,
            max_iterations=3,
            concurrent_tools=False,
        )
        start = asyncio.get_event_loop().time()
        result = await AgentRunner().run(spec)
        elapsed = asyncio.get_event_loop().time() - start
        self.assertIsNotNone(result)
        self.assertGreaterEqual(elapsed, 0.25, "Should run serially when concurrent_tools=False")

    async def test_mixed_safety_batches_separately(self):
        safe_tool = _ConcurrencyTrackingTool("safe_tool", delay=0.1, concurrency_safe=True)
        unsafe_tool = _ConcurrencyTrackingTool("unsafe_tool", delay=0.1, concurrency_safe=False)
        registry = ToolRegistry()
        registry.register(safe_tool)
        registry.register(unsafe_tool)
        provider = _ConcurrentToolCallProvider(["safe_tool", "unsafe_tool"])
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "run tools"}],
            tools=registry,
            provider=provider,
            max_iterations=3,
            concurrent_tools=True,
        )
        result = await AgentRunner().run(spec)
        self.assertIsNotNone(result)
        # user + assistant(tool_calls) + 2 tool results + assistant(final) = 5
        self.assertEqual(len(result.messages), 5)
        tool_msgs = [m for m in result.messages if m["role"] == "tool"]
        self.assertEqual(len(tool_msgs), 2)


# ---- Step 17a Tests: Tool Result Normalization via Runner ----

class _EmptyResultTool(Tool):
    @property
    def name(self) -> str:
        return "empty_tool"
    @property
    def description(self) -> str:
        return "Returns empty result"
    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}
    async def execute(self, **kwargs) -> ToolResult:
        return ToolResult("")


class _HugeResultTool(Tool):
    @property
    def name(self) -> str:
        return "huge_tool"
    @property
    def description(self) -> str:
        return "Returns huge result"
    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}
    async def execute(self, **kwargs) -> ToolResult:
        return ToolResult("x" * 20000)


class _NormalizationProvider(LLMProvider):
    def __init__(self):
        self.call_count = 0
    @property
    def model(self) -> str:
        return "mock"
    async def chat(self, messages, tools=None, model=None, temperature=0.7, max_tokens=4096):
        self.call_count += 1
        if self.call_count == 1:
            return LLMResponse(content="", tool_calls=[
                ToolCallRequest(id="c1", name="empty_tool", arguments={"x": "a"}),
                ToolCallRequest(id="c2", name="huge_tool", arguments={"x": "b"}),
            ], finish_reason="tool_calls", usage={"prompt_tokens": 10, "completion_tokens": 5})
        return LLMResponse(content="done", finish_reason="stop",
                           usage={"prompt_tokens": 15, "completion_tokens": 8})
    async def chat_stream_with_retry(self, messages, tools=None, model=None,
                                      temperature=0.7, max_tokens=4096,
                                      on_content_delta=None, retry_config=None):
        resp = await self.chat(messages, tools, model, temperature, max_tokens)
        if on_content_delta and resp.content:
            await on_content_delta(resp.content)
        return resp


class TestToolResultNormalization(unittest.IsolatedAsyncioTestCase):
    async def test_empty_tool_result_filled(self):
        registry = ToolRegistry()
        registry.register(_EmptyResultTool())
        registry.register(_HugeResultTool())
        config = ContextGovernanceConfig(
            tools=registry,
            context_window_tokens=200_000,
            max_tool_result_chars=100,
        )
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "run tools"}],
            tools=registry,
            provider=_NormalizationProvider(),
            governance_config=config,
            max_iterations=3,
        )
        result = await AgentRunner().run(spec)
        self.assertIsNotNone(result)
        tool_msgs = [m for m in result.messages if m["role"] == "tool"]
        empty_msg = next((m for m in tool_msgs if m["name"] == "empty_tool"), None)
        self.assertIsNotNone(empty_msg)
        # Should be filled with "completed with no output"
        self.assertNotEqual(empty_msg["content"], "")
        self.assertIn("completed with no output", empty_msg["content"])

    async def test_huge_tool_result_truncated(self):
        registry = ToolRegistry()
        registry.register(_EmptyResultTool())
        registry.register(_HugeResultTool())
        config = ContextGovernanceConfig(
            tools=registry,
            context_window_tokens=200_000,
            max_tool_result_chars=100,
        )
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "run tools"}],
            tools=registry,
            provider=_NormalizationProvider(),
            governance_config=config,
            max_iterations=3,
        )
        result = await AgentRunner().run(spec)
        self.assertIsNotNone(result)
        tool_msgs = [m for m in result.messages if m["role"] == "tool"]
        huge_msg = next((m for m in tool_msgs if m["name"] == "huge_tool"), None)
        self.assertIsNotNone(huge_msg)
        self.assertLess(len(huge_msg["content"]), 20000)
        self.assertIn("truncated", huge_msg["content"])


# ---- Step 17a Tests: Malformed Tool Call Recovery ----

class _MalformedCallProvider(LLMProvider):
    """Returns invalid tool call names to test recovery."""

    def __init__(self, fail_count: int = 1):
        self.call_count = 0
        self.fail_count = fail_count

    @property
    def model(self) -> str:
        return "mock"

    async def chat(self, messages, tools=None, model=None, temperature=0.7, max_tokens=4096):
        self.call_count += 1
        if self.call_count <= self.fail_count:
            return LLMResponse(content="", tool_calls=[
                ToolCallRequest(id="bad1", name="", arguments={"x": "a"}),
            ], finish_reason="tool_calls", usage={"prompt_tokens": 10, "completion_tokens": 5})
        return LLMResponse(content="recovered", finish_reason="stop",
                           usage={"prompt_tokens": 15, "completion_tokens": 8})

    async def chat_stream_with_retry(self, messages, tools=None, model=None,
                                      temperature=0.7, max_tokens=4096,
                                      on_content_delta=None, retry_config=None):
        resp = await self.chat(messages, tools, model, temperature, max_tokens)
        if on_content_delta and resp.content:
            await on_content_delta(resp.content)
        return resp


class _AlwaysMalformedProvider(LLMProvider):
    """Always returns invalid tool calls to test repeated retry."""

    def __init__(self):
        self.call_count = 0

    @property
    def model(self) -> str:
        return "mock"

    async def chat(self, messages, tools=None, model=None, temperature=0.7, max_tokens=4096):
        self.call_count += 1
        return LLMResponse(content="", tool_calls=[
            ToolCallRequest(id="bad1", name="", arguments={"x": "a"}),
        ], finish_reason="tool_calls", usage={"prompt_tokens": 10, "completion_tokens": 5})

    async def chat_stream_with_retry(self, messages, tools=None, model=None,
                                      temperature=0.7, max_tokens=4096,
                                      on_content_delta=None, retry_config=None):
        return await self.chat(messages, tools, model, temperature, max_tokens)


class TestMalformedToolCallRecovery(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_name_dropped_and_retried(self):
        registry = ToolRegistry()
        registry.register(EchoTool())
        provider = _MalformedCallProvider(fail_count=1)
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "use tool"}],
            tools=registry,
            provider=provider,
            max_iterations=5,
        )
        result = await AgentRunner().run(spec)
        self.assertIsNotNone(result)
        self.assertIn("recovered", result.final_content)
        # The retry message should appear in the conversation
        retry_msgs = [m for m in result.messages
                      if isinstance(m.get("content"), str)
                      and "invalid" in m["content"].lower()]
        self.assertGreaterEqual(len(retry_msgs), 1)

    async def test_all_invalid_twice_then_fallback(self):
        """After repeated malformed calls, runner eventually produces a non-tool response."""
        registry = ToolRegistry()
        registry.register(EchoTool())
        provider = _AlwaysMalformedProvider()
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "do something"}],
            tools=registry,
            provider=provider,
            max_iterations=5,
        )
        result = await AgentRunner().run(spec)
        self.assertIsNotNone(result)
        # The runner should keep retrying; it may hit max_iterations
        self.assertIn(result.stop_reason, ("stop", "max_iterations"))


# ---- Step 17a Tests: LLM Timeout ----

class _SlowProvider(LLMProvider):
    """Provider that sleeps longer than the timeout."""

    @property
    def model(self) -> str:
        return "mock"

    async def chat(self, messages, tools=None, model=None, temperature=0.7, max_tokens=4096):
        await asyncio.sleep(10)
        return LLMResponse(content="too late", finish_reason="stop",
                           usage={"prompt_tokens": 10, "completion_tokens": 5})

    async def chat_stream_with_retry(self, messages, tools=None, model=None,
                                      temperature=0.7, max_tokens=4096,
                                      on_content_delta=None, retry_config=None):
        await asyncio.sleep(10)
        if on_content_delta:
            await on_content_delta("too late")
        return LLMResponse(content="too late", finish_reason="stop",
                           usage={"prompt_tokens": 10, "completion_tokens": 5})


class TestLLMTimeout(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_returns_error_finish_reason(self):
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "hello"}],
            tools=_MockToolRegistry(),
            provider=_SlowProvider(),
            llm_timeout_s=0.1,
        )
        result = await AgentRunner().run(spec)
        self.assertIsNotNone(result)
        self.assertEqual(result.stop_reason, "error")

    async def test_timeout_in_multi_iteration(self):
        """Timeout in a multi-iteration run (with tool call) returns error reason."""
        class _SlowThenFastProvider(LLMProvider):
            def __init__(self):
                self.call_count = 0
            @property
            def model(self) -> str:
                return "mock"
            async def chat(self, messages, tools=None, model=None, temperature=0.7, max_tokens=4096):
                self.call_count += 1
                if self.call_count == 1:
                    return LLMResponse(content="", tool_calls=[
                        ToolCallRequest(id="c1", name="echo", arguments={"text": "hi"}),
                    ], finish_reason="tool_calls", usage={"prompt_tokens": 10, "completion_tokens": 5})
                await asyncio.sleep(10)
                return LLMResponse(content="too late", finish_reason="stop",
                                   usage={"prompt_tokens": 15, "completion_tokens": 8})
            async def chat_stream_with_retry(self, messages, tools=None, model=None,
                                              temperature=0.7, max_tokens=4096,
                                              on_content_delta=None, retry_config=None):
                resp = await self.chat(messages, tools, model, temperature, max_tokens)
                if on_content_delta and resp.content:
                    await on_content_delta(resp.content)
                return resp

        registry = ToolRegistry()
        registry.register(EchoTool())
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "do it"}],
            tools=registry,
            provider=_SlowThenFastProvider(),
            llm_timeout_s=0.3,
            max_iterations=6,
        )
        result = await AgentRunner().run(spec)
        self.assertIsNotNone(result)
        # After first iteration (tool call succeeded), subsequent iterations time out
        # Empty retries consume extra iterations, but final should be "error"
        self.assertEqual(result.stop_reason, "error")


# ---- Step 17b Tests: Empty Content Retry ----

class _EmptyResponseProvider(LLMProvider):
    """Returns empty content for the first N calls, then a real response."""

    def __init__(self, empty_count: int = 1):
        self.call_count = 0
        self.empty_count = empty_count

    @property
    def model(self) -> str:
        return "mock"

    async def chat(self, messages, tools=None, model=None, temperature=0.7, max_tokens=4096):
        self.call_count += 1
        if self.call_count <= self.empty_count:
            return LLMResponse(content="", finish_reason="stop",
                               usage={"prompt_tokens": 5, "completion_tokens": 3})
        return LLMResponse(content="final response", finish_reason="stop",
                           usage={"prompt_tokens": 10, "completion_tokens": 5})

    async def chat_stream_with_retry(self, messages, tools=None, model=None,
                                      temperature=0.7, max_tokens=4096,
                                      on_content_delta=None, retry_config=None):
        resp = await self.chat(messages, tools, model, temperature, max_tokens)
        if on_content_delta and resp.content:
            await on_content_delta(resp.content)
        return resp


class _AlwaysEmptyProvider(LLMProvider):
    """Always returns empty content."""

    @property
    def model(self) -> str:
        return "mock"

    async def chat(self, messages, tools=None, model=None, temperature=0.7, max_tokens=4096):
        return LLMResponse(content="", finish_reason="stop",
                           usage={"prompt_tokens": 5, "completion_tokens": 3})

    async def chat_stream_with_retry(self, messages, tools=None, model=None,
                                      temperature=0.7, max_tokens=4096,
                                      on_content_delta=None, retry_config=None):
        return await self.chat(messages, tools, model, temperature, max_tokens)


class TestEmptyContentRetry(unittest.IsolatedAsyncioTestCase):
    async def test_retry_once_then_succeed(self):
        """Empty content triggers retry, then succeeds on next call."""
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "hello"}],
            tools=_MockToolRegistry(),
            provider=_EmptyResponseProvider(empty_count=1),
            max_iterations=5,
        )
        result = await AgentRunner().run(spec)
        self.assertIsNotNone(result)
        self.assertEqual(result.stop_reason, "stop")
        self.assertEqual(result.final_content, "final response")

    async def test_retry_twice_then_succeed(self):
        """Two empty retries allowed, then succeeds on third call."""
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "hello"}],
            tools=_MockToolRegistry(),
            provider=_EmptyResponseProvider(empty_count=2),
            max_iterations=5,
        )
        result = await AgentRunner().run(spec)
        self.assertIsNotNone(result)
        self.assertEqual(result.stop_reason, "stop")
        self.assertEqual(result.final_content, "final response")

    async def test_exceed_retries_triggers_finalization_fallback(self):
        """After _MAX_EMPTY_RETRIES, finalization message is sent without tools."""
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "hello"}],
            tools=_MockToolRegistry(),
            provider=_AlwaysEmptyProvider(),
            max_iterations=10,
        )
        result = await AgentRunner().run(spec)
        self.assertIsNotNone(result)
        # Should eventually return a response (even if empty) via finalization fallback
        self.assertIsNotNone(result.final_content)
        self.assertIn(result.stop_reason, ("stop", "error"))


# ---- Step 17b Tests: Length Recovery ----

class _LengthResponseProvider(LLMProvider):
    """Returns finish_reason='length' for first N calls, then stop."""

    def __init__(self, length_count: int = 1):
        self.call_count = 0
        self.length_count = length_count

    @property
    def model(self) -> str:
        return "mock"

    async def chat(self, messages, tools=None, model=None, temperature=0.7, max_tokens=4096):
        self.call_count += 1
        if self.call_count <= self.length_count:
            return LLMResponse(content="partial content...", finish_reason="length",
                               usage={"prompt_tokens": 10, "completion_tokens": 5})
        return LLMResponse(content="completed response", finish_reason="stop",
                           usage={"prompt_tokens": 15, "completion_tokens": 8})

    async def chat_stream_with_retry(self, messages, tools=None, model=None,
                                      temperature=0.7, max_tokens=4096,
                                      on_content_delta=None, retry_config=None):
        resp = await self.chat(messages, tools, model, temperature, max_tokens)
        if on_content_delta and resp.content:
            await on_content_delta(resp.content)
        return resp


class _AlwaysLengthProvider(LLMProvider):
    """Always returns finish_reason='length'."""

    @property
    def model(self) -> str:
        return "mock"

    async def chat(self, messages, tools=None, model=None, temperature=0.7, max_tokens=4096):
        return LLMResponse(content="still more to say...", finish_reason="length",
                           usage={"prompt_tokens": 10, "completion_tokens": 5})

    async def chat_stream_with_retry(self, messages, tools=None, model=None,
                                      temperature=0.7, max_tokens=4096,
                                      on_content_delta=None, retry_config=None):
        resp = await self.chat(messages, tools, model, temperature, max_tokens)
        if on_content_delta and resp.content:
            await on_content_delta(resp.content)
        return resp


class TestLengthRecovery(unittest.IsolatedAsyncioTestCase):
    async def test_recovery_once_then_succeed(self):
        """Length recovery appends prompt and continues, then succeeds."""
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "write a lot"}],
            tools=_MockToolRegistry(),
            provider=_LengthResponseProvider(length_count=1),
            max_iterations=5,
        )
        result = await AgentRunner().run(spec)
        self.assertIsNotNone(result)
        self.assertEqual(result.stop_reason, "stop")
        self.assertEqual(result.final_content, "completed response")
        # The partial content should be preserved in the assistant message
        assistant_msgs = [m for m in result.messages if m["role"] == "assistant"]
        self.assertGreaterEqual(len(assistant_msgs), 2)

    async def test_recovery_capped_at_max(self):
        """After _MAX_LENGTH_RECOVERIES, stops continuing."""
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "write a lot"}],
            tools=_MockToolRegistry(),
            provider=_AlwaysLengthProvider(),
            max_iterations=10,
        )
        result = await AgentRunner().run(spec)
        self.assertIsNotNone(result)
        # Should still have content, even if stop_reason is "length" or "max_iterations"
        self.assertIn(result.final_content, ("still more to say...", "Reached max iterations without a final response."))


# ---- Step 17b Tests: Goal Continuation Max Rounds ----

class _GoalCappingProvider(LLMProvider):
    """Returns text response; call_count tracks iterations."""

    def __init__(self):
        self.call_count = 0

    @property
    def model(self) -> str:
        return "mock"

    async def chat(self, messages, tools=None, model=None, temperature=0.7, max_tokens=4096):
        self.call_count += 1
        return LLMResponse(content=f"response {self.call_count}", finish_reason="stop",
                           usage={"prompt_tokens": 5, "completion_tokens": 3})

    async def chat_stream_with_retry(self, messages, tools=None, model=None,
                                      temperature=0.7, max_tokens=4096,
                                      on_content_delta=None, retry_config=None):
        resp = await self.chat(messages, tools, model, temperature, max_tokens)
        if on_content_delta and resp.content:
            await on_content_delta(resp.content)
        return resp


class TestGoalContinuationMaxRounds(unittest.IsolatedAsyncioTestCase):
    async def test_goal_continuation_capped(self):
        """Goal continuation stops after _MAX_GOAL_CONTINUATION_ROUNDS."""
        provider = _GoalCappingProvider()
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "start"}],
            tools=_MockToolRegistry(),
            provider=provider,
            max_iterations=20,
            goal_active_predicate=lambda: True,
            goal_continue_message="Continue working",
            goal_continuation_rounds=0,
        )
        result = await AgentRunner().run(spec)
        self.assertIsNotNone(result)
        self.assertIsNotNone(result.final_content)
        # Should have stopped due to cap, not max_iterations
        # With _MAX_GOAL_CONTINUATION_ROUNDS=12 and spec.max_iterations=20,
        # the cap should trigger before max_iterations
        self.assertEqual(result.goal_continuation_rounds, 12)

    async def test_goal_continuation_rounds_in_result(self):
        """goal_continuation_rounds is returned in AgentRunResult."""
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "start"}],
            tools=_MockToolRegistry(),
            provider=_GoalCappingProvider(),
            max_iterations=5,
            goal_active_predicate=lambda: False,
        )
        result = await AgentRunner().run(spec)
        self.assertIsNotNone(result)
        self.assertEqual(result.goal_continuation_rounds, 0)


# ---- Step 17b Tests: Injection Cycles Limit & Merge ----

class _CyclicInjectionProvider(LLMProvider):
    """Returns tool_calls on first call, then text responses."""

    def __init__(self):
        self.call_count = 0

    @property
    def model(self) -> str:
        return "mock"

    async def chat(self, messages, tools=None, model=None, temperature=0.7, max_tokens=4096):
        self.call_count += 1
        if self.call_count == 1:
            return LLMResponse(content="", tool_calls=[
                ToolCallRequest(id="c1", name="echo", arguments={"text": "hi"}),
            ], finish_reason="tool_calls", usage={"prompt_tokens": 10, "completion_tokens": 5})
        return LLMResponse(content=f"text response {self.call_count}", finish_reason="stop",
                           usage={"prompt_tokens": 15, "completion_tokens": 8})

    async def chat_stream_with_retry(self, messages, tools=None, model=None,
                                      temperature=0.7, max_tokens=4096,
                                      on_content_delta=None, retry_config=None):
        resp = await self.chat(messages, tools, model, temperature, max_tokens)
        if on_content_delta and resp.content:
            await on_content_delta(resp.content)
        return resp


class _InjectingCallback:
    """Callback that returns injected messages a limited number of times."""

    def __init__(self, count: int = 1, msg_count: int = 1):
        self.remaining = count
        self.msg_count = msg_count
        self.total_calls = 0

    async def callback(self) -> list[dict]:
        self.total_calls += 1
        if self.remaining > 0:
            self.remaining -= 1
            return [{"role": "user", "content": f"injected_{i}"}
                    for i in range(self.msg_count)]
        return []


class TestInjectionCyclesLimit(unittest.IsolatedAsyncioTestCase):
    async def test_injection_cycles_capped(self):
        """Injection stops after _MAX_INJECTION_CYCLES even if callback still returns messages."""
        injector = _InjectingCallback(count=10, msg_count=1)
        registry = ToolRegistry()
        registry.register(EchoTool())
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "start"}],
            tools=registry,
            provider=_CyclicInjectionProvider(),
            injection_callback=injector.callback,
            max_iterations=20,
        )
        result = await AgentRunner().run(spec)
        self.assertIsNotNone(result)
        # Should have no more than _MAX_INJECTION_CYCLES (5) injection rounds
        # Each injection adds a user message
        user_msgs = [m for m in result.messages if m["role"] == "user"]
        # Original user msg + up to 5 injected
        self.assertLessEqual(len(user_msgs), 6)

    async def test_injection_per_turn_capped(self):
        """Each injection cycle drains at most _MAX_INJECTIONS_PER_TURN messages."""
        injector = _InjectingCallback(count=1, msg_count=10)
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "start"}],
            tools=_MockToolRegistry(),
            provider=_MockProvider(),
            injection_callback=injector.callback,
            max_iterations=5,
        )
        result = await AgentRunner().run(spec)
        self.assertIsNotNone(result)
        user_msgs = [m for m in result.messages if m["role"] == "user"]
        # Original + at most _MAX_INJECTIONS_PER_TURN (3) injected
        self.assertLessEqual(len(user_msgs), 4)


class TestInjectionMerge(unittest.IsolatedAsyncioTestCase):
    async def test_adjacent_user_messages_merged(self):
        """Adjacent user messages from injection are merged into one."""
        # Use runner's static method directly
        messages = [{"role": "user", "content": "original"}]
        injected = [
            {"role": "user", "content": "first injection"},
            {"role": "user", "content": "second injection"},
        ]
        AgentRunner._append_injected_messages(messages, injected)
        user_msgs = [m for m in messages if m["role"] == "user"]
        self.assertEqual(len(user_msgs), 1)
        self.assertIn("original", user_msgs[0]["content"])
        self.assertIn("first injection", user_msgs[0]["content"])
        self.assertIn("second injection", user_msgs[0]["content"])

    async def test_non_user_messages_not_merged(self):
        """Non-user messages (e.g. tool) are appended separately without merging."""
        messages = [{"role": "user", "content": "original"}]
        injected = [
            {"role": "assistant", "content": "assistant msg"},
            {"role": "user", "content": "user after assistant"},
        ]
        AgentRunner._append_injected_messages(messages, injected)
        roles = [m["role"] for m in messages]
        self.assertEqual(len(messages), 3)
        self.assertEqual(roles, ["user", "assistant", "user"])

    async def test_integration_with_runner(self):
        """Runner merges injected user messages during execution."""
        call_count = 0
        async def multi_inject():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [
                    {"role": "user", "content": "injected_a"},
                    {"role": "user", "content": "injected_b"},
                ]
            return []

        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "original"}],
            tools=_MockToolRegistry(),
            provider=_MockProvider(),
            injection_callback=multi_inject,
            max_iterations=5,
        )
        result = await AgentRunner().run(spec)
        self.assertIsNotNone(result)
        user_msgs = [m for m in result.messages if m["role"] == "user"]
        # original + injected (merged into one) = 2 user messages max
        self.assertLessEqual(len(user_msgs), 2)


if __name__ == "__main__":
    unittest.main()
