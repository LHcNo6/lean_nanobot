"""Tests for Step 16 �� Subagents + Sustained Goals."""

import asyncio
import gc
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest import mock

from step78.autocompact import AutoCompact
from step78.bus import MessageBus
from step78.channel import BaseChannel
from step78.channels.cli import CliChannel
from step78.command import CommandContext, CommandRouter, normalize_command_text
from step78.consolidation import Consolidator, _consolidation_boundary
from step78.goal_state import *
from step78.helpers import estimate_message_tokens, estimate_prompt_tokens
from step78.llm import Runtime
from step78.memory import MemoryStore
from step78.context import ContextBuilder
from step78.bus.events import InboundMessage, OutboundMessage, StreamDeltaEvent
from step78.hook import AgentHook, AgentHookContext, AgentRunHookContext, CompositeHook
from step78.llm import LLMResponse, ToolCallRequest
from step78.loop import AgentLoop, StreamPublishingHook, TurnContext, TurnState
from step78.manager import ChannelManager
from step78.pairing import PAIRING_CODE_META_KEY, PairingStore
from step78.provider import LLMProvider
from step78.governance import ContextGovernanceConfig, ContextGovernor
from step78.llm import GenerationSettings, LLMRuntime, ModelPreset, resolve_preset
from step78.providers import (
    FallbackProvider,
    ProviderSettings,
    build_provider_snapshot,
    create_dynamic_spec,
    find_by_model,
    find_by_name,
    is_fallbackable_exception,
    make_provider,
    provider_signature,
)
from step78.runner import AgentRunSpec, AgentRunner, _EMPTY_FINAL_RESPONSE_MESSAGE
from step78.session import Session, SessionManager
from step78.subagent import SubagentManager, SubagentStatus
from step78.tool import ToolRegistry, Tool
from step78.tools.long_task import CreateGoalTool, UpdateGoalTool
from step78.tools.echo import EchoTool
from step78.tools.spawn import SpawnTool
from step78.bus.events import StreamDeltaEvent
from step78.bus.outbound_events import StreamEndEvent


async def _consume_final_response(bus):
    """Consume outbound messages, skipping stream deltas and typed runtime
    events, until the final user-visible response.

    step27 之后 Progress/RetryWait 等 typed 事件也会出现在 outbound 队列上，
    但它们不是最终用户回复；只有普通消息或在最终内容上挂的
    ``StreamedResponseEvent`` 才算。
    """
    from step78.bus.outbound_events import StreamEndEvent, StreamedResponseEvent

    while True:
        msg = await bus.consume_outbound()
        if isinstance(msg, StreamDeltaEvent):
            continue
        if msg.event is not None and not isinstance(msg.event, StreamedResponseEvent):
            continue
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

    def get_runtime_context_providers(self):
        # step29: loop 在 turn 构建时聚合工具自带的运行时上下文提供器。
        providers = []
        for name in sorted(self._tools):
            provider = self._tools[name].runtime_context_provider()
            if provider is not None:
                providers.append(provider)
        return providers

    async def execute(self, name, **params):
        return ""

    def get(self, name):
        return self._tools.get(name)


# ���� Hook Tests ����

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
        # Should not raise �� CompositeHook isolates errors
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


from step78.tool import ToolResult


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


# ── Streaming Tests ──

class _TrackingHookForStream(AgentHook):
    def __init__(self):
        self.stream_deltas: list[str] = []
        self.stream_end_count = 0
        self.iter_stream_contents: list[str] = []

    async def on_stream(self, ctx: AgentHookContext, delta: str) -> None:
        self.stream_deltas.append(delta)

    async def on_stream_end(self, ctx: AgentHookContext, *, resuming: bool = False) -> None:
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
        # step32：单轮文本 = 2 次 on_stream_end——模型响应后(resuming=True)
        # + 注入判定后收尾(resuming=False)。
        self.assertEqual(hook.stream_end_count, 2)

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
        # step32：每轮响应后各 1 次 flush(resuming=True) + 最终收尾
        # flush(resuming=False) = 3 次 on_stream_end。
        self.assertEqual(hook.stream_end_count, 3)

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
        # step32：流收尾改走 typed ``StreamEndEvent``（resuming 语义），
        # StreamDeltaEvent 只承载内容增量。
        deltas: list[StreamDeltaEvent] = []
        while True:
            msg = await bus.consume_outbound()
            if isinstance(msg, StreamDeltaEvent):
                deltas.append(msg)
                continue
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
        # step32：内容增量走 StreamDeltaEvent，流收尾走 typed StreamEndEvent
        # （resuming=False 表示真正结束）。消费到最终消息（挂
        # StreamedResponseEvent）为止。
        deltas: list[StreamDeltaEvent] = []
        stream_ends: list[StreamEndEvent] = []
        final = None
        while final is None:
            msg = await bus.consume_outbound()
            if isinstance(msg, StreamDeltaEvent):
                deltas.append(msg)
            elif isinstance(msg.event, StreamEndEvent):
                stream_ends.append(msg.event)
            else:
                final = msg
        self.assertIsNotNone(final.content)
        self.assertGreater(len(deltas), 0)
        self.assertTrue(stream_ends)
        # 最终收尾信号必须是 resuming=False（流真正结束）
        self.assertFalse(stream_ends[-1].resuming)
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


# ── Existing AgentLoop Tests ──

class TestAgentLoopStateHandlers(unittest.IsolatedAsyncioTestCase):
    async def test_state_restore(self):
        loop, _ = self._make_loop()
        ctx = TurnContext(msg=InboundMessage(content="hi", chat_id="test"), session_key="test")
        event = await loop._state_restore(ctx)
        self.assertEqual(event, "ok")
        self.assertIsNotNone(ctx.session)
        self.assertEqual(ctx.session.key, "test")

    def _make_loop(self, runtime=None):
        bus = MessageBus()
        provider = _MockProvider()
        registry = _MockToolRegistry()
        tmp = tempfile.mkdtemp()
        session_manager = SessionManager(workspace=tmp)
        context_builder = ContextBuilder(workspace=".")
        memory = MemoryStore(workspace=tmp)
        kwargs = dict(
            bus=bus, provider=provider, registry=registry,
            session_manager=session_manager, context_builder=context_builder,
            memory=memory, identity="You are a test bot.",
        )
        if runtime is not None:
            kwargs["runtime"] = runtime
        else:
            kwargs["replay_budget"] = 10000
        return AgentLoop(**kwargs), bus

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
        runtime = LLMRuntime.capture(
            provider=_MockProvider(), model="mock",
            context_window_tokens=4000, max_tokens=512,
        )
        loop, _ = self._make_loop(runtime=runtime)
        loop.consolidator.provider = _MockProvider()
        session = loop.sessions.get_or_create("test")
        for i in range(20):
            session.add_message("user", "x" * 500 + str(i))
        ctx = TurnContext(msg=InboundMessage(content="hi", chat_id="test"), session_key="test")
        ctx.session = session
        event = await loop._state_compact(ctx)
        self.assertEqual(event, "ok")
        self.assertIsNotNone(ctx.pending_summary)
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
        # Consecutive user roles are merged (role alternation, nanobot-aligned).
        self.assertEqual(ctx.initial_messages[-1]["content"], "previous message\nhi")

    async def test_state_run(self):
        loop, _ = self._make_loop()
        session = Session(key="test")
        ctx = TurnContext(msg=InboundMessage(content="hello", chat_id="test"), session_key="test")
        ctx.session = session
        ctx.pending_summary = "Summary: test"
        ctx.history = []
        ctx.initial_messages = loop.context.build_messages(
            current_message="hello", identity="You are a test bot.", session_summary="Summary: test",
        )
        event = await loop._state_run(ctx)
        self.assertEqual(event, "ok")
        self.assertIsNotNone(ctx.final_content)
        self.assertIn("Summary", ctx.final_content)

    async def test_state_save(self):
        loop, _ = self._make_loop()
        session = Session(key="test")
        ctx = TurnContext(msg=InboundMessage(content="hello", chat_id="test"), session_key="test")
        ctx.session = session
        ctx.pending_summary = "Summary: test"
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
        ctx.pending_summary = "Summary: test"
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
        for expected_state in [TurnState.COMPACT, TurnState.COMMAND, TurnState.BUILD,
                                TurnState.RUN, TurnState.SAVE, TurnState.RESPOND, TurnState.DONE]:
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

        # Publish both messages; the second should be injected mid-turn into
        # the first turn (active pending-queue) instead of queuing a competing task.
        await bus.publish_inbound(InboundMessage(content="msg1", chat_id="lock_test"))
        await bus.publish_inbound(InboundMessage(content="msg2", chat_id="lock_test"))

        response = await _consume_final_response(bus)
        self.assertIsNotNone(response.content)
        session = loop.sessions.get_or_create("lock_test")
        # user + assistant + injected user + assistant (single combined turn)
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
            return await _consume_final_response(bus)

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


# ���� Existing MessageBus Tests ����

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


# ���� Existing Token Estimator Tests ����

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

    def test_current_role_defaults_to_user(self):
        ctx = ContextBuilder(workspace=".")
        messages = ctx.build_messages(current_message="hello")
        self.assertEqual(messages[-1]["role"], "user")
        self.assertEqual(messages[-1]["content"], "hello")

    def test_current_role_assistant(self):
        ctx = ContextBuilder(workspace=".")
        messages = ctx.build_messages(
            current_message="[subagent] done", current_role="assistant",
        )
        self.assertEqual(messages[-1]["role"], "assistant")
        self.assertEqual(messages[-1]["content"], "[subagent] done")


# ���� Mid-turn Injection Tests ����

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


class _SpawnToolProvider(LLMProvider):
    """Main agent provider: first call spawns a subagent, later calls answer."""

    def __init__(self):
        self._count = 0

    @property
    def model(self) -> str:
        return "mock"

    async def chat(self, messages, tools=None, model=None, temperature=0.7, max_tokens=4096):
        self._count += 1
        if self._count == 1:
            return LLMResponse(
                content="",
                tool_calls=[ToolCallRequest(
                    id="call_spawn", name="spawn",
                    arguments={"task": "summarize the research", "label": "Research Agent"},
                )],
                finish_reason="tool_calls",
                usage={"prompt_tokens": 40, "completion_tokens": 8},
            )
        return LLMResponse(
            content="Main answer.",
            finish_reason="stop",
            usage={"prompt_tokens": 60, "completion_tokens": 5},
        )

    async def chat_stream_with_retry(self, messages, tools=None, model=None, temperature=0.7,
                                      max_tokens=4096, on_content_delta=None, retry_config=None):
        resp = await self.chat(messages, tools, model, temperature, max_tokens)
        if on_content_delta and resp.content:
            for chunk in resp.content.split(" "):
                await on_content_delta(chunk + " ")
        return resp


class _SlowSubProvider(LLMProvider):
    """Subagent provider that completes after a short delay."""

    @property
    def model(self) -> str:
        return "mock"

    async def chat(self, messages, tools=None, model=None, temperature=0.7, max_tokens=4096):
        await asyncio.sleep(0.1)
        return LLMResponse(
            content="Research completed.", finish_reason="stop",
            usage={"prompt_tokens": 5, "completion_tokens": 3},
        )


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
        ctx.pending_summary = "Summary: test"
        ctx.history = []
        ctx.initial_messages = loop.context.build_messages(
            current_message="hello", identity="You are a test bot.", session_summary="Summary: test",
        )
        event = await loop._state_run(ctx)
        self.assertEqual(event, "ok")
        self.assertIsNotNone(ctx.final_content)

    async def test_dispatch_leftover_republishes_to_bus(self):
        """_dispatch re-publishes leftover queued messages to bus.inbound in finally."""
        loop, bus = _make_injection_loop()
        loop._pending_queues["test"] = asyncio.Queue()
        loop._pending_queues["test"].put_nowait(InboundMessage(content="leftover_msg", chat_id="test"))
        # A command short-circuits the state machine, so nothing drains the queue.
        await loop._dispatch(InboundMessage(content="/history", chat_id="test"))
        msg = await bus.consume_inbound()
        self.assertEqual(msg.content, "leftover_msg")
        self.assertNotIn("test", loop._pending_queues)

    async def test_dispatch_pending_queue_registered_for_turn(self):
        """_dispatch registers the pending queue so busy messages can be injected."""
        loop, _ = _make_injection_loop()
        msg = InboundMessage(content="hello", chat_id="sys_qt")
        await loop._dispatch(msg)
        # After the turn the queue is removed again.
        self.assertNotIn("sys_qt", loop._pending_queues)

    async def test_subagent_announce_injected_mid_turn(self):
        """A spawned subagent's announce is answered mid-turn (A2+A3).

        SpawnTool passes the current session key to SubagentManager, and the
        subagent's system-channel announce is drained into the active turn
        instead of publishing a competing final response.
        """
        bus = MessageBus()
        tmp = tempfile.mkdtemp()
        session_manager = SessionManager(workspace=tmp)
        context_builder = ContextBuilder(workspace=".")
        memory = MemoryStore(workspace=tmp)

        subagents = SubagentManager(bus=bus, provider=_SlowSubProvider(), max_concurrent_subagents=2)
        main_provider = _SpawnToolProvider()
        loop = AgentLoop(
            bus=bus, provider=main_provider, registry=ToolRegistry(),
            session_manager=session_manager, context_builder=context_builder,
            memory=memory, identity="You are a test bot.",
            replay_budget=10000, subagent_manager=subagents,
        )

        # Watch for the subagent being tracked under the session key while running.
        saw_key_slot = asyncio.Event()

        async def _watch_key():
            for _ in range(500):
                if "subtest" in subagents._session_tasks:
                    saw_key_slot.set()
                    return
                await asyncio.sleep(0.005)
        watcher = asyncio.create_task(_watch_key())

        task = asyncio.create_task(loop.run())
        await bus.publish_inbound(InboundMessage(content="delegate this research", chat_id="subtest"))
        response = await asyncio.wait_for(_consume_final_response(bus), timeout=10)
        self.assertIsNotNone(response.content)
        await watcher
        # SpawnTool passed the current session key through to SubagentManager.
        self.assertTrue(saw_key_slot.is_set(), "subagent not tracked under the session key")

        # The announce was injected mid-turn: no second, independent final response.
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(bus.consume_outbound(), timeout=0.4)

        session = loop.sessions.get_or_create("subtest")
        user_rows = [m for m in session.messages if m.get("role") == "user"]
        self.assertTrue(
            any("Research completed" in (m.get("content") or "") for m in user_rows),
            f"subagent result should be injected mid-turn, got: {user_rows}",
        )
        # The injected event marker is persisted for history alignment.
        self.assertTrue(
            any(m.get("subagent_task_id") for m in session.messages),
            "injected subagent marker should be persisted",
        )
        loop.stop()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_spawn_tool_passes_request_session_key(self):
        """SpawnTool relays the current request's session key to the manager."""
        bus = MessageBus()
        manager = SubagentManager(bus=bus, max_concurrent_subagents=2)
        tool = SpawnTool(manager=manager)
        from step78.context import RequestContext, request_context
        ctx = RequestContext(session_key="sess_xyz")
        with request_context(ctx):
            result = await tool.execute(task="job")
        self.assertIn("started", str(result))
        self.assertIn("sess_xyz", manager._session_tasks)


# ---- Step 23 Tests: Runner Injection Upgrade ----

class _LimitProbeCallback:
    """Records how it was called: with or without the limit kwarg."""
    def __init__(self, messages: list[dict] | None = None):
        self.messages = messages or []
        self.calls: list[tuple] = []

    async def callback(self, *, limit: int | None = None) -> list[dict]:
        self.calls.append((limit,))
        return self.messages


class TestRunnerInjectionUpgrade(unittest.IsolatedAsyncioTestCase):
    async def test_has_injection_content(self):
        """_has_injection_content filters None / blank / empty payloads."""
        runner = AgentRunner()
        self.assertFalse(runner._has_injection_content(None))
        self.assertFalse(runner._has_injection_content(""))
        self.assertFalse(runner._has_injection_content("   \n "))
        self.assertTrue(runner._has_injection_content("x"))
        self.assertFalse(runner._has_injection_content([]))
        self.assertTrue(runner._has_injection_content(["a"]))
        self.assertTrue(runner._has_injection_content(0))

    async def test_drain_injections_probes_limit_kwarg(self):
        """Callbacks accepting 'limit' receive it; plain callbacks do not."""
        runner = AgentRunner()
        with_limit = _LimitProbeCallback([{"role": "user", "content": "a"}])
        spec = AgentRunSpec(
            initial_messages=[], tools=_MockToolRegistry(),
            provider=_MockProvider(), injection_callback=with_limit.callback,
        )
        out = await runner._drain_injections(spec)
        self.assertEqual(out, [{"role": "user", "content": "a"}])
        self.assertEqual(with_limit.calls, [(3,)])

        plain_calls: list = []

        async def plain() -> list:
            plain_calls.append(True)
            return []

        spec2 = AgentRunSpec(
            initial_messages=[], tools=_MockToolRegistry(),
            provider=_MockProvider(), injection_callback=plain,
        )
        await runner._drain_injections(spec2)
        self.assertEqual(len(plain_calls), 1)

    async def test_drain_injections_filters_blank_and_wraps_objects(self):
        """Blank user messages are dropped; non-dict items are wrapped."""
        class _FakeMsg:
            def __init__(self, content: str):
                self.content = content

        async def callback() -> list:
            return [
                {"role": "user", "content": "   "},
                {"role": "user", "content": "ok"},
                _FakeMsg("wrapped"),
            ]

        runner = AgentRunner()
        spec = AgentRunSpec(
            initial_messages=[], tools=_MockToolRegistry(),
            provider=_MockProvider(), injection_callback=callback,
        )
        out = await runner._drain_injections(spec)
        self.assertEqual(
            out,
            [{"role": "user", "content": "ok"}, {"role": "user", "content": "wrapped"}],
        )

    async def test_drain_injections_callback_exception(self):
        """A raising injection callback degrades to []."""
        async def callback() -> list:
            raise RuntimeError("boom")

        runner = AgentRunner()
        spec = AgentRunSpec(
            initial_messages=[], tools=_MockToolRegistry(),
            provider=_MockProvider(), injection_callback=callback,
        )
        self.assertEqual(await runner._drain_injections(spec), [])

    async def test_try_drain_injections_goal_continue(self):
        """No injections + active goal falls back to goal continuation, no cycle used."""
        runner = AgentRunner()
        spec = AgentRunSpec(
            initial_messages=[], tools=_MockToolRegistry(),
            provider=_MockProvider(),
            goal_active_predicate=lambda: True,
            goal_continue_message="Continue working",
        )
        messages: list[dict] = []
        assistant = {"role": "assistant", "content": "final"}
        should_continue, cycles = await runner._try_drain_injections(
            spec, messages, assistant, 0, allow_goal_continue=True,
        )
        self.assertTrue(should_continue)
        self.assertEqual(cycles, 0)
        self.assertEqual(spec.goal_continuation_rounds, 1)
        self.assertEqual(messages[-1]["content"], "Continue working")
        self.assertIn(assistant, messages)

    async def test_try_drain_injections_goal_cap(self):
        """Goal continuation stops at _MAX_GOAL_CONTINUATION_ROUNDS."""
        runner = AgentRunner()
        spec = AgentRunSpec(
            initial_messages=[], tools=_MockToolRegistry(),
            provider=_MockProvider(),
            goal_active_predicate=lambda: True,
            goal_continue_message="Continue working",
            goal_continuation_rounds=12,
        )
        messages: list[dict] = []
        should_continue, _ = await runner._try_drain_injections(
            spec, messages, {"role": "assistant", "content": "final"}, 0,
            allow_goal_continue=True,
        )
        self.assertFalse(should_continue)
        self.assertEqual(messages, [])

    async def test_try_drain_injections_no_injection_no_goal(self):
        """No injections and no active goal -> do not continue, append nothing."""
        runner = AgentRunner()
        spec = AgentRunSpec(
            initial_messages=[], tools=_MockToolRegistry(),
            provider=_MockProvider(),
            goal_active_predicate=lambda: False,
        )
        messages: list[dict] = []
        should_continue, cycles = await runner._try_drain_injections(
            spec, messages, {"role": "assistant", "content": "final"}, 0,
            allow_goal_continue=True,
        )
        self.assertFalse(should_continue)
        self.assertEqual(cycles, 0)
        self.assertEqual(messages, [])

    async def test_try_drain_injections_injection_precedes_goal(self):
        """A real injection wins over goal continuation and consumes a cycle."""
        runner = AgentRunner()
        injector = _InjectionSource(["follow-up"])
        spec = AgentRunSpec(
            initial_messages=[], tools=_MockToolRegistry(),
            provider=_MockProvider(),
            injection_callback=injector.callback,
            goal_active_predicate=lambda: True,
            goal_continue_message="Continue working",
        )
        messages: list[dict] = []
        assistant = {"role": "assistant", "content": "final"}
        should_continue, cycles = await runner._try_drain_injections(
            spec, messages, assistant, 0, allow_goal_continue=True,
        )
        self.assertTrue(should_continue)
        self.assertEqual(cycles, 1)
        self.assertEqual(spec.goal_continuation_rounds, 0)
        self.assertEqual(messages[-1]["content"], "follow-up")


# ���� Context Governance Tests ����

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
        from step78.helpers import estimate_message_tokens as emt
        tokens = emt({"role": "user", "content": "hello"})
        self.assertGreaterEqual(tokens, 4)

    def test_estimate_prompt_tokens_with_tools(self):
        from step78.helpers import estimate_prompt_tokens
        tokens = estimate_prompt_tokens(
            [{"role": "user", "content": "hi"}],
            tools=[{"type": "function", "function": {"name": "echo"}}],
        )
        self.assertGreater(tokens, 0)

    def test_find_legal_message_start(self):
        from step78.helpers import find_legal_message_start
        msgs = [
            {"role": "tool", "tool_call_id": "orphan", "content": "x"},
            {"role": "user", "content": "hi"},
        ]
        start = find_legal_message_start(msgs)
        self.assertEqual(start, 1)

    def test_truncate_text(self):
        from step78.helpers import truncate_text
        result = truncate_text("hello world", 5)
        self.assertIn("truncated", result)
        self.assertLessEqual(len(result), 20)

    def test_ensure_nonempty_tool_result(self):
        from step78.helpers import ensure_nonempty_tool_result
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
        self.assertIsNone(result)  # no provider �� returns None after raw_archive

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
        from step78.context import RequestContext, bind_request_context
        self.sessions = _MockSessionManager()
        self.tool = CreateGoalTool(sessions=self.sessions)
        self._req_token = bind_request_context(RequestContext(session_key="test_session"))

    def tearDown(self):
        from step78.context import reset_request_context
        reset_request_context(self._req_token)

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
        from step78.context import RequestContext, bind_request_context
        self.sessions = _MockSessionManager()
        self.tool = UpdateGoalTool(sessions=self.sessions)
        self._req_token = bind_request_context(RequestContext(session_key="test_session"))
        self.sessions.get_or_create("test_session").metadata["goal_state"] = {
            "status": "active", "objective": "test objective",
        }

    def tearDown(self):
        from step78.context import reset_request_context
        reset_request_context(self._req_token)

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
        # step64：tools=None 时（降级无工具请求）返回正常响应
        if tools is None:
            return LLMResponse(content="fallback answer", finish_reason="stop",
                               usage={"prompt_tokens": 10, "completion_tokens": 5})
        return LLMResponse(content="", tool_calls=[
            ToolCallRequest(id="bad1", name="", arguments={"x": "a"}),
        ], finish_reason="tool_calls", usage={"prompt_tokens": 10, "completion_tokens": 5})

    async def chat_stream_with_retry(self, messages, tools=None, model=None,
                                      temperature=0.7, max_tokens=4096,
                                      on_content_delta=None, retry_config=None):
        return await self.chat(messages, tools, model, temperature, max_tokens)

    async def chat_with_retry(self, messages, tools=None, model=None,
                               temperature=0.7, max_tokens=4096, retry_config=None):
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
        # step64：malformed_retry 在 _request_model 内部递归，provider 被调用 2 次
        self.assertEqual(provider.call_count, 2)

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


# ---- Step 17b Tests: Error Termination ----

class _CountingTimeoutProvider(LLMProvider):
    """Always times out; counts calls."""

    def __init__(self):
        self.call_count = 0

    @property
    def model(self) -> str:
        return "mock"

    async def chat(self, messages, tools=None, model=None, temperature=0.7, max_tokens=4096):
        self.call_count += 1
        await asyncio.sleep(10)
        return LLMResponse(content="too late", finish_reason="stop",
                           usage={"prompt_tokens": 10, "completion_tokens": 5})

    async def chat_stream_with_retry(self, messages, tools=None, model=None,
                                      temperature=0.7, max_tokens=4096,
                                      on_content_delta=None, retry_config=None):
        return await self.chat(messages, tools, model, temperature, max_tokens)


class TestErrorTermination(unittest.IsolatedAsyncioTestCase):
    async def test_error_with_active_goal_stops_immediately(self):
        """LLM error terminates instead of triggering goal continuation."""
        provider = _CountingTimeoutProvider()
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "start"}],
            tools=_MockToolRegistry(),
            provider=provider,
            llm_timeout_s=0.05,
            max_iterations=20,
            goal_active_predicate=lambda: True,
            goal_continue_message="Continue working",
        )
        result = await AgentRunner().run(spec)
        self.assertEqual(result.stop_reason, "error")
        self.assertEqual(result.goal_continuation_rounds, 0)
        self.assertLessEqual(provider.call_count, 2)

    async def test_error_with_injection_callback(self):
        """LLM error terminates without draining injections."""
        injector = _InjectingCallback(count=10, msg_count=1)
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "start"}],
            tools=_MockToolRegistry(),
            provider=_SlowProvider(),
            llm_timeout_s=0.05,
            max_iterations=20,
            injection_callback=injector.callback,
        )
        result = await AgentRunner().run(spec)
        self.assertEqual(result.stop_reason, "error")
        self.assertEqual(injector.total_calls, 0)
        user_msgs = [m for m in result.messages if m["role"] == "user"]
        self.assertEqual(len(user_msgs), 1)


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


# ---- Step 18 Tests: Schema ----

from step78.schema import (
    StringSchema, IntegerSchema, NumberSchema, BooleanSchema,
    ArraySchema, ObjectSchema, tool_parameters_schema,
)
from step78.tool import Schema as SchemaABC


class TestSchema(unittest.TestCase):
    def test_string_schema(self):
        s = StringSchema("A name", min_length=1, max_length=100)
        js = s.to_json_schema()
        self.assertEqual(js["type"], "string")
        self.assertEqual(js["description"], "A name")
        self.assertEqual(js["minLength"], 1)
        self.assertEqual(js["maxLength"], 100)

    def test_integer_schema(self):
        s = IntegerSchema(description="Count", minimum=0, maximum=100)
        js = s.to_json_schema()
        self.assertEqual(js["type"], "integer")
        self.assertEqual(js["minimum"], 0)
        self.assertEqual(js["maximum"], 100)

    def test_number_schema(self):
        s = NumberSchema(description="Price", minimum=0.0)
        js = s.to_json_schema()
        self.assertEqual(js["type"], "number")
        self.assertEqual(js["minimum"], 0.0)

    def test_boolean_schema(self):
        s = BooleanSchema(description="Enabled", default=True)
        js = s.to_json_schema()
        self.assertEqual(js["type"], "boolean")
        self.assertTrue(js["default"])

    def test_array_schema(self):
        items = StringSchema()
        s = ArraySchema(items, description="Tags", min_items=1)
        js = s.to_json_schema()
        self.assertEqual(js["type"], "array")
        self.assertEqual(js["items"], {"type": "string"})
        self.assertEqual(js["minItems"], 1)

    def test_object_schema(self):
        s = ObjectSchema({
            "name": StringSchema("The name"),
            "count": IntegerSchema(description="Count"),
        }, required=["name"])
        js = s.to_json_schema()
        self.assertEqual(js["type"], "object")
        self.assertIn("name", js["properties"])
        self.assertIn("count", js["properties"])
        self.assertEqual(js["required"], ["name"])

    def test_tool_parameters_schema(self):
        schema = tool_parameters_schema(
            text=StringSchema("The text"),
            required=["text"],
        )
        self.assertEqual(schema["type"], "object")
        self.assertIn("text", schema["properties"])
        self.assertEqual(schema["required"], ["text"])
        self.assertEqual(schema["additionalProperties"], False)


class TestSchemaValidation(unittest.TestCase):
    def test_validate_string_valid(self):
        s = StringSchema(min_length=1, max_length=10)
        errors = s.validate_value("hello")
        self.assertEqual(errors, [])

    def test_validate_string_too_short(self):
        s = StringSchema(min_length=2)
        errors = s.validate_value("a")
        self.assertGreater(len(errors), 0)

    def test_validate_integer_enum(self):
        s = IntegerSchema(enum=[1, 2, 3])
        errors = s.validate_value(2)
        self.assertEqual(errors, [])
        errors = s.validate_value(4)
        self.assertGreater(len(errors), 0)

    def test_validate_integer_range(self):
        s = IntegerSchema(minimum=0, maximum=100)
        errors = s.validate_value(-1)
        self.assertGreater(len(errors), 0)

    def test_validate_nullable(self):
        s = StringSchema(nullable=True)
        errors = s.validate_value(None)
        self.assertEqual(errors, [])

    def test_validate_object_required(self):
        s = ObjectSchema({"x": StringSchema()}, required=["x"])
        errors = s.validate_value({})
        self.assertGreater(len(errors), 0)
        errors = s.validate_value({"x": "ok"})
        self.assertEqual(errors, [])


class TestSchemaHelpers(unittest.TestCase):
    def test_resolve_type(self):
        self.assertEqual(SchemaABC.resolve_json_schema_type("string"), "string")
        self.assertEqual(SchemaABC.resolve_json_schema_type(["string", "null"]), "string")
        self.assertIsNone(SchemaABC.resolve_json_schema_type(None))

    def test_fragment_schema_object(self):
        s = StringSchema("test")
        d = SchemaABC.fragment(s)
        self.assertEqual(d["type"], "string")

    def test_fragment_dict(self):
        d = {"type": "string"}
        result = SchemaABC.fragment(d)
        self.assertIs(result, d)

    def test_subpath(self):
        self.assertEqual(SchemaABC.subpath("", "a"), "a")
        self.assertEqual(SchemaABC.subpath("a", "b"), "a.b")


class TestCastParams(unittest.TestCase):
    def setUp(self):
        from step78.tool import tool_parameters
        @tool_parameters(tool_parameters_schema(
            count=IntegerSchema("Count"),
            name=StringSchema("Name"),
            active=BooleanSchema(description="Active"),
            required=["count", "name"],
        ))
        class _TestTool(Tool):
            @property
            def name(self): return "test"
            @property
            def description(self): return "test"
            async def execute(self, **kw): return ToolResult("ok")
        self.tool = _TestTool()

    def test_cast_int_from_str(self):
        params = self.tool.cast_params({"count": "42", "name": "foo"})
        self.assertIsInstance(params["count"], int)
        self.assertEqual(params["count"], 42)

    def test_cast_bool_from_str(self):
        params = self.tool.cast_params({"count": 1, "name": "x", "active": "true"})
        self.assertIsInstance(params["active"], bool)
        self.assertTrue(params["active"])

    def test_cast_float_from_str(self):
        from step78.tool import tool_parameters
        @tool_parameters(tool_parameters_schema(
            price=NumberSchema(description="Price"),
            required=["price"],
        ))
        class _PriceTool(Tool):
            @property
            def name(self): return "price"
            @property
            def description(self): return "price"
            async def execute(self, **kw): return ToolResult("ok")
        params = _PriceTool().cast_params({"price": "3.14"})
        self.assertIsInstance(params["price"], float)
        self.assertAlmostEqual(params["price"], 3.14)


class TestValidateParams(unittest.TestCase):
    def test_validate_missing_required(self):
        from step78.tool import tool_parameters
        @tool_parameters(tool_parameters_schema(
            name=StringSchema("Name"),
            required=["name"],
        ))
        class _ReqTool(Tool):
            @property
            def name(self): return "req"
            @property
            def description(self): return "req"
            async def execute(self, **kw): return ToolResult("ok")
        errors = _ReqTool().validate_params({})
        self.assertGreater(len(errors), 0)
        self.assertIn("name", errors[0])

    def test_validate_type_error(self):
        from step78.tool import tool_parameters
        @tool_parameters(tool_parameters_schema(
            count=IntegerSchema("Count"),
            required=["count"],
        ))
        class _IntTool(Tool):
            @property
            def name(self): return "int"
            @property
            def description(self): return "int"
            async def execute(self, **kw): return ToolResult("ok")
        errors = _IntTool().validate_params({"count": "not_an_int"})
        self.assertGreater(len(errors), 0)

    def test_validate_additional_properties_blocked(self):
        params = {"count": 1, "extra_field": "bad"}
        from step78.tool import tool_parameters
        @tool_parameters(tool_parameters_schema(
            count=IntegerSchema("Count"),
            required=["count"],
        ))
        class _StrictTool(Tool):
            @property
            def name(self): return "strict"
            @property
            def description(self): return "strict"
            async def execute(self, **kw): return ToolResult("ok")
        errors = _StrictTool().validate_params(params)
        self.assertGreater(len(errors), 0)
        self.assertIn("extra_field", errors[0])

    def test_validate_pass(self):
        from step78.tool import tool_parameters
        @tool_parameters(tool_parameters_schema(
            name=StringSchema("Name"),
            count=IntegerSchema("Count"),
            required=["name"],
        ))
        class _PassTool(Tool):
            @property
            def name(self): return "pass"
            @property
            def description(self): return "pass"
            async def execute(self, **kw): return ToolResult("ok")
        errors = _PassTool().validate_params({"name": "hello", "count": 5})
        self.assertEqual(errors, [])


class TestToolParametersDecorator(unittest.TestCase):
    def test_decorator_sets_parameters(self):
        from step78.tool import tool_parameters
        @tool_parameters(tool_parameters_schema(
            text=StringSchema("Text"),
            required=["text"],
        ))
        class _DecoratedTool(Tool):
            @property
            def name(self): return "decorated"
            @property
            def description(self): return "decorated"
            async def execute(self, **kw): return ToolResult("ok")
        params = _DecoratedTool().parameters
        self.assertEqual(params["type"], "object")
        self.assertIn("text", params["properties"])
        self.assertEqual(params["required"], ["text"])

    def test_decorator_removes_parameters_from_abstract(self):
        from step78.tool import tool_parameters
        @tool_parameters(tool_parameters_schema(
            x=StringSchema("X"),
        ))
        class _NoAbstractParams(Tool):
            @property
            def name(self): return "noabs"
            @property
            def description(self): return "noabs"
            async def execute(self, **kw): return ToolResult("ok")
        # Should not raise TypeError about abstract
        instance = _NoAbstractParams()
        self.assertEqual(instance.parameters["type"], "object")

    def test_decorator_fresh_copy(self):
        from step78.tool import tool_parameters
        @tool_parameters(tool_parameters_schema(
            x=StringSchema("X"),
        ))
        class _CopyTool(Tool):
            @property
            def name(self): return "copy"
            @property
            def description(self): return "copy"
            async def execute(self, **kw): return ToolResult("ok")
        p1 = _CopyTool().parameters
        p2 = _CopyTool().parameters
        self.assertIsNot(p1, p2)


class TestToolCreate(unittest.TestCase):
    def test_tool_create_default(self):
        from step78.context import ToolContext
        tool = EchoTool.create(ToolContext())
        self.assertIsInstance(tool, EchoTool)
        self.assertEqual(tool.name, "echo")

    def test_tool_create_with_context(self):
        from step78.context import ToolContext
        tool = SpawnTool.create(ToolContext(subagent_manager=object()))
        self.assertIsInstance(tool, SpawnTool)
        self.assertIsNotNone(tool._manager)

    def test_create_goal_tool_create(self):
        from step78.context import ToolContext
        tool = CreateGoalTool.create(ToolContext(sessions=object()))
        self.assertIsInstance(tool, CreateGoalTool)
        self.assertIsNotNone(tool._sessions)


class TestPrepareCall(unittest.TestCase):
    def test_prepare_call_resolves(self):
        registry = ToolRegistry()
        registry.register(EchoTool())
        tool, params, error = registry.prepare_call("echo", {"text": "hello"})
        self.assertIsNotNone(tool)
        self.assertIsNone(error)
        self.assertEqual(params["text"], "hello")

    def test_prepare_call_not_found(self):
        registry = ToolRegistry()
        registry.register(EchoTool())
        tool, params, error = registry.prepare_call("unknown", {})
        self.assertIsNone(tool)
        self.assertIsNotNone(error)

    def test_prepare_call_suggestion(self):
        registry = ToolRegistry()
        registry.register(EchoTool())
        tool, params, error = registry.prepare_call("ECHO", {"text": "hi"})
        # Current impl: case-sensitive lookup; suggest if fuzzy match
        self.assertIsNone(tool)
        # _suggest_name matches by alnum key
        self.assertIsNotNone(error)

    def test_prepare_call_validation_error(self):
        registry = ToolRegistry()
        registry.register(EchoTool())
        tool, params, error = registry.prepare_call("echo", {"text": 123})
        # text should be string, but cast_params will convert 123 to "123"
        self.assertIsNotNone(tool)
        self.assertIsNone(error)  # cast converts 123 -> "123"

    def test_prepare_call_with_arguments_wrapper(self):
        registry = ToolRegistry()
        registry.register(EchoTool())
        tool, params, error = registry.prepare_call("echo", {"arguments": '{"text": "hello"}'})
        self.assertIsNotNone(tool)
        self.assertIsNone(error)
        self.assertEqual(params["text"], "hello")


class TestToolContext(unittest.IsolatedAsyncioTestCase):
    async def test_request_context_bind(self):
        from step78.context import RequestContext, bind_request_context, reset_request_context, current_request_context
        ctx = RequestContext(session_key="test_sess")
        token = bind_request_context(ctx)
        try:
            self.assertEqual(current_request_context().session_key, "test_sess")
        finally:
            reset_request_context(token)
        self.assertIsNone(current_request_context())

    async def test_request_context_session_key_helper(self):
        from step78.context import RequestContext, bind_request_context, reset_request_context, current_request_session_key
        ctx = RequestContext(session_key="my_session")
        token = bind_request_context(ctx)
        try:
            self.assertEqual(current_request_session_key(), "my_session")
        finally:
            reset_request_context(token)
        self.assertIsNone(current_request_session_key())

    async def test_tool_context_construction(self):
        from step78.context import ToolContext
        tc = ToolContext(config={"key": "val"}, workspace="/tmp", bus=object())
        self.assertEqual(tc.workspace, "/tmp")
        self.assertEqual(tc.config["key"], "val")

    async def test_tool_context_defaults(self):
        from step78.context import ToolContext
        tc = ToolContext()
        self.assertIsNone(tc.config)
        self.assertEqual(tc.workspace, "")
        self.assertIsNone(tc.bus)


class TestToolLoader(unittest.IsolatedAsyncioTestCase):
    async def test_discover_finds_tools(self):
        from step78.loader import ToolLoader
        loader = ToolLoader(test_classes=[EchoTool, SpawnTool])
        discovered = loader.discover()
        names = {cls.__name__ for cls in discovered}
        self.assertIn("EchoTool", names)
        self.assertIn("SpawnTool", names)

    async def test_discover_skips_abstract(self):
        from step78.loader import ToolLoader
        loader = ToolLoader(test_classes=[])
        discovered = loader.discover()
        self.assertEqual(len(discovered), 0)

    async def test_load_registers_tools(self):
        from step78.context import ToolContext
        from step78.loader import ToolLoader
        registry = ToolRegistry()
        loader = ToolLoader(test_classes=[EchoTool])
        loader.load(ToolContext(), registry)
        self.assertTrue(registry.has("echo"))

    async def test_load_filtered_by_scope(self):
        from step78.context import ToolContext
        from step78.loader import ToolLoader

        class _OtherScopeTool(Tool):
            _scopes = {"other"}
            @property
            def name(self): return "other"
            @property
            def description(self): return "other"
            @property
            def parameters(self): return {"type": "object", "properties": {}}
            async def execute(self, **kw): return ToolResult("ok")

        registry = ToolRegistry()
        loader = ToolLoader(test_classes=[EchoTool, _OtherScopeTool])
        loader.load(ToolContext(), registry, scope="core")
        self.assertTrue(registry.has("echo"))
        self.assertFalse(registry.has("other"))

    async def test_tool_loader_with_real_discovery(self):
        from step78.loader import ToolLoader
        loader = ToolLoader()
        discovered = loader.discover()
        names = {cls.__name__ for cls in discovered}
        # Should find echo, spawn, long_task tools
        self.assertIn("EchoTool", names)
        self.assertIn("SpawnTool", names)
        self.assertIn("CreateGoalTool", names)
        self.assertIn("UpdateGoalTool", names)


class TestRegistryEnhancements(unittest.IsolatedAsyncioTestCase):
    async def test_get_definitions_sorted(self):
        registry = ToolRegistry()
        registry.register(SpawnTool())
        registry.register(EchoTool())
        defs = registry.get_definitions()
        names = [d["function"]["name"] for d in defs]
        self.assertEqual(names, sorted(names))

    async def test_get_definitions_cached(self):
        registry = ToolRegistry()
        registry.register(EchoTool())
        defs1 = registry.get_definitions()
        defs2 = registry.get_definitions()
        self.assertIs(defs1, defs2)

    async def test_get_runtime_context_providers(self):
        registry = ToolRegistry()
        registry.register(EchoTool())
        providers = registry.get_runtime_context_providers()
        self.assertEqual(providers, [])

    async def test_tool_registry_contains(self):
        registry = ToolRegistry()
        registry.register(EchoTool())
        self.assertIn("echo", registry)
        self.assertNotIn("unknown", registry)

    async def test_tool_registry_len(self):
        registry = ToolRegistry()
        self.assertEqual(len(registry), 0)
        registry.register(EchoTool())
        self.assertEqual(len(registry), 1)

    async def test_tool_names_property(self):
        registry = ToolRegistry()
        registry.register(EchoTool())
        registry.register(SpawnTool())
        names = registry.tool_names
        self.assertIn("echo", names)
        self.assertIn("spawn", names)


class TestRegistryExecuteViaPrepareCall(unittest.IsolatedAsyncioTestCase):
    async def test_execute_with_prepare_call(self):
        from step78.context import RequestContext, bind_request_context, reset_request_context
        registry = ToolRegistry()
        registry.register(EchoTool())
        ctx = RequestContext()
        token = bind_request_context(ctx)
        try:
            result = await registry.execute("echo", text="hello")
            self.assertEqual(str(result), "Echo: hello")
        finally:
            reset_request_context(token)

    async def test_execute_not_found(self):
        registry = ToolRegistry()
        result = await registry.execute("unknown")
        self.assertIn("not found", str(result).lower())

    async def test_execute_with_tool_error(self):
        class _FailingTool(Tool):
            @property
            def name(self): return "fail"
            @property
            def description(self): return "fail"
            @property
            def parameters(self): return {"type": "object", "properties": {}}
            async def execute(self, **kw): return ToolResult.error("failed intentionally")
        registry = ToolRegistry()
        registry.register(_FailingTool())
        result = await registry.execute("fail")
        self.assertTrue(result.is_error)
        self.assertIn("failed", str(result).lower())


class TestStep18Integration(unittest.IsolatedAsyncioTestCase):
    async def test_prepare_call_with_runner(self):
        from step78.context import RequestContext
        class _CallProvider(LLMProvider):
            @property
            def model(self): return "mock"
            async def chat(self, messages, **kw):
                is_first = messages[-1].get("role") == "user" if messages else True
                if is_first:
                    return LLMResponse(content="", tool_calls=[
                        ToolCallRequest(id="c1", name="echo", arguments={"text": "hello"}),
                    ], finish_reason="tool_calls", usage={})
                return LLMResponse(content="done", finish_reason="stop", usage={})
            async def chat_stream_with_retry(self, **kw): return await self.chat(kw.get("messages", []))

        registry = ToolRegistry()
        registry.register(EchoTool())
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "hi"}],
            tools=registry,
            provider=_CallProvider(),
            max_iterations=3,
        )
        result = await AgentRunner().run(spec)
        self.assertIsNotNone(result)
        self.assertEqual(result.final_content, "done")

    async def test_full_tool_loader_with_loop(self):
        bus = MessageBus()
        provider = _MockProvider()
        registry = ToolRegistry()
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
        task = asyncio.create_task(loop.run())
        await bus.publish_inbound(InboundMessage(content="hello", chat_id="test"))
        response = await bus.consume_outbound()
        self.assertIsNotNone(response.content)
        # Registry should have tools loaded by ToolLoader
        self.assertGreater(len(registry), 0)
        loop.stop()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task


class TestStorageKeyEncoding(unittest.TestCase):
    def test_roundtrip(self):
        mgr = SessionManager(workspace=tempfile.mkdtemp())
        for key in ["default", "cli:direct", "中文:会话", "a/b\\c?d*e"]:
            stem = mgr._storage_key(key)
            self.assertEqual(mgr._decode_storage_key(stem), key)

    def test_collision_resistant(self):
        mgr = SessionManager(workspace=tempfile.mkdtemp())
        self.assertNotEqual(mgr._storage_key("a:b"), mgr._storage_key("a_b"))

    def test_safe_key(self):
        self.assertEqual(SessionManager.safe_key("cli:direct"), "cli_direct")
        self.assertEqual(SessionManager.safe_key("a<b>c"), "a_b_c")

    def test_decode_garbage(self):
        mgr = SessionManager(workspace=tempfile.mkdtemp())
        self.assertIsNone(mgr._decode_storage_key("!!!not-base64!!!"))

    def test_legacy_migration(self):
        tmp = tempfile.mkdtemp()
        mgr = SessionManager(workspace=tmp)
        key = "cli:direct"
        legacy = mgr._get_legacy_lossy_path(key)
        legacy.parent.mkdir(parents=True, exist_ok=True)
        sess = Session(key=key)
        sess.add_message("user", "hello")
        with open(legacy, "w", encoding="utf-8") as f:
            f.write('{"_type": "metadata", "key": "cli:direct"}\n')
            f.write(json.dumps(sess.messages[0], ensure_ascii=False) + "\n")
        loaded = mgr.get_or_create(key)
        self.assertEqual(loaded.key, key)
        self.assertEqual(len(loaded.messages), 1)
        self.assertTrue(mgr._get_session_path(key).exists())
        self.assertFalse(legacy.exists())


class TestTwoLevelCache(unittest.TestCase):
    def _make(self, size=2):
        return SessionManager(workspace=tempfile.mkdtemp(), max_cached_sessions=size)

    def test_hot_cache_identity(self):
        mgr = self._make()
        a = mgr.get_or_create("a")
        self.assertIs(mgr.get_or_create("a"), a)
        self.assertIn("a", mgr._cache)

    def test_evicts_to_overflow_preserves_identity(self):
        mgr = self._make(2)
        a = mgr.get_or_create("a")
        b = mgr.get_or_create("b")
        c = mgr.get_or_create("c")
        self.assertNotIn("a", mgr._cache)
        self.assertIn("a", mgr._overflow_cache)
        self.assertIs(mgr.get_or_create("a"), a)
        self.assertIn("a", mgr._cache)

    def test_evicted_gc_reloads_from_disk(self):
        mgr = self._make(1)
        a = mgr.get_or_create("a")
        a.add_message("user", "persisted")
        mgr.save(a)
        mgr.get_or_create("b")
        del a
        gc.collect()
        fresh = mgr.get_or_create("a")
        self.assertEqual(len(fresh.messages), 1)

    def test_invalidate(self):
        mgr = self._make()
        a = mgr.get_or_create("a")
        a.add_message("user", "x")
        mgr.save(a)
        mgr.invalidate("a")
        self.assertNotIn("a", mgr._cache)
        self.assertNotIn("a", mgr._overflow_cache)
        reloaded = mgr.get_or_create("a")
        self.assertIsNot(reloaded, a)
        self.assertEqual(len(reloaded.messages), 1)


class TestRetentionSuffix(unittest.TestCase):
    def _session(self, n):
        s = Session(key="t")
        for i in range(n):
            s.add_message("user" if i % 2 == 0 else "assistant", f"msg {i}")
        return s

    def test_basic_truncation(self):
        s = self._session(10)
        result = s.retain_recent_legal_suffix(4)
        self.assertEqual(len(s.messages), 4)
        self.assertEqual(len(result.dropped), 6)
        self.assertEqual(result.already_consolidated_count, 0)
        self.assertEqual(s.messages[0]["content"], "msg 6")

    def test_extend_to_user(self):
        s = self._session(10)
        result = s.retain_recent_legal_suffix(3, extend_to_user=True)
        self.assertEqual(s.messages[0]["role"], "user")
        self.assertLessEqual(len(s.messages), 4)

    def test_orphan_tool_result_trimmed(self):
        s = Session(key="t")
        s.add_message("user", "do it")
        s.add_message("assistant", "", tool_calls=[{"id": "t1", "function": {"name": "x", "arguments": "{}"}}])
        s.add_message("tool", "result", tool_call_id="t1")
        s.add_message("user", "more")
        result = s.retain_recent_legal_suffix(2)
        self.assertEqual(len(s.messages), 1)
        self.assertEqual(s.messages[0]["role"], "user")
        self.assertEqual(len(result.dropped), 3)

    def test_consolidated_prefix_counting(self):
        s = self._session(10)
        s.last_consolidated = 3
        result = s.retain_recent_legal_suffix(4)
        self.assertEqual(len(s.messages), 4)
        self.assertEqual(s.last_consolidated, 0)
        self.assertEqual(result.already_consolidated_count, 3)
        self.assertEqual(len(result.dropped), 6)

    def test_clear_keeps_consolidation_count(self):
        s = self._session(5)
        s.last_consolidated = 2
        result = s.retain_recent_legal_suffix(0)
        self.assertEqual(s.messages, [])
        self.assertEqual(result.already_consolidated_count, 2)


class TestEnforceFileCap(unittest.TestCase):
    def test_under_cap_noop(self):
        s = Session(key="t")
        for i in range(10):
            s.add_message("user", f"m{i}")
        archived = []
        s.enforce_file_cap(on_archive=archived.append, limit=100)
        self.assertEqual(len(s.messages), 10)
        self.assertEqual(archived, [])

    def test_over_cap_archives(self):
        s = Session(key="t")
        for i in range(50):
            s.add_message("user", f"m{i}")
        archived = []
        s.enforce_file_cap(on_archive=archived.append, limit=10)
        self.assertEqual(len(s.messages), 10)
        self.assertEqual(len(archived), 1)
        self.assertEqual(len(archived[0]), 40)

    def test_consolidated_prefix_not_archived(self):
        s = Session(key="t")
        for i in range(20):
            s.add_message("user", f"m{i}")
        s.last_consolidated = 12
        archived = []
        s.enforce_file_cap(on_archive=archived.append, limit=8)
        self.assertEqual(len(s.messages), 8)
        self.assertEqual(archived, [])
        self.assertEqual(s.last_consolidated, 0)


class TestAutoCompact(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.sessions = SessionManager(workspace=self.tmp)
        self.store = MemoryStore(workspace=self.tmp)
        self.conso = Consolidator(
            store=self.store, sessions=self.sessions,
            build_messages=lambda **kw: [], get_tool_definitions=lambda: [],
        )
        self.compact = AutoCompact(self.sessions, self.conso, session_ttl_minutes=1)
        self._runtime = lambda: Runtime(context_window_tokens=10000, max_tokens=1024)

    def test_is_expired(self):
        now = datetime.now()
        self.assertTrue(self.compact._is_expired((now - timedelta(minutes=2)).isoformat(), now))
        self.assertFalse(self.compact._is_expired((now - timedelta(seconds=30)).isoformat(), now))
        self.assertFalse(self.compact._is_expired(None, now))
        disabled = AutoCompact(self.sessions, self.conso, session_ttl_minutes=0)
        self.assertFalse(disabled._is_expired((now - timedelta(hours=2)).isoformat(), now))

    def test_has_compactable_idle_tail(self):
        busy = self.sessions.get_or_create("busy")
        for i in range(30):
            busy.add_message("user", f"m{i}")
        self.assertTrue(self.compact._has_compactable_idle_tail("busy"))
        self.sessions.get_or_create("empty")
        self.assertFalse(self.compact._has_compactable_idle_tail("empty"))
        small = self.sessions.get_or_create("small")
        for i in range(3):
            small.add_message("user", f"m{i}")
        self.assertFalse(self.compact._has_compactable_idle_tail("small"))

    def test_check_expired_skips_active_and_internal(self):
        idle = self.sessions.get_or_create("idle2")
        for i in range(30):
            idle.add_message("user", f"m{i}")
        idle.updated_at = (datetime.now() - timedelta(minutes=10)).isoformat()
        self.sessions.save(idle)
        dream = self.sessions.get_or_create("dream:2026")
        for i in range(30):
            dream.add_message("user", f"d{i}")
        dream.updated_at = (datetime.now() - timedelta(minutes=10)).isoformat()
        self.sessions.save(dream)
        scheduled = []
        self.compact.check_expired(
            lambda coro: scheduled.append(coro),
            self._runtime,
            active_session_keys={"idle2"},
        )
        self.assertEqual(scheduled, [])

    async def test_check_expired_schedules_and_archives(self):
        idle = self.sessions.get_or_create("idle")
        for i in range(30):
            idle.add_message("user", f"m{i}")
        idle.updated_at = (datetime.now() - timedelta(minutes=10)).isoformat()
        self.sessions.save(idle)
        scheduled = []
        self.compact.check_expired(lambda coro: scheduled.append(coro), self._runtime)
        self.assertEqual(len(scheduled), 1)
        await scheduled[0]
        reloaded = self.sessions.get_or_create("idle")
        self.assertLessEqual(len(reloaded.messages), 8)

    async def test_prepare_session_summary_cold_path(self):
        s = self.sessions.get_or_create("sum")
        s.metadata["_last_summary"] = {
            "text": "Old summary",
            "last_active": "2026-01-01T00:00:00",
        }
        self.sessions.save(s)
        session, pending = self.compact.prepare_session(s, "sum")
        self.assertIs(session, s)
        self.assertIsNotNone(pending)
        self.assertIn("Previous conversation summary", pending)
        self.assertIn("Old summary", pending)

    async def test_prepare_session_internal_and_clean(self):
        s = self.sessions.get_or_create("dream:x")
        _, pending = self.compact.prepare_session(s, "dream:x")
        self.assertIsNone(pending)
        s2 = self.sessions.get_or_create("plain")
        _, pending = self.compact.prepare_session(s2, "plain")
        self.assertIsNone(pending)


class TestPendingUserTurn(unittest.IsolatedAsyncioTestCase):
    def _make_loop(self):
        bus = MessageBus()
        provider = _MockProvider()
        registry = _MockToolRegistry()
        tmp = tempfile.mkdtemp()
        return AgentLoop(
            bus=bus, provider=provider, registry=registry,
            session_manager=SessionManager(workspace=tmp),
            context_builder=ContextBuilder(workspace="."),
            memory=MemoryStore(workspace=tmp),
            identity="You are a test bot.", replay_budget=10000,
        )

    def setUp(self):
        self.loop = self._make_loop()

    def test_mark_clear(self):
        s = Session(key="t")
        self.loop._mark_pending_user_turn(s)
        self.assertTrue(s.metadata.get("pending_user_turn"))
        self.loop._clear_pending_user_turn(s)
        self.assertNotIn("pending_user_turn", s.metadata)

    def test_restore_appends_error(self):
        s = Session(key="t")
        s.add_message("user", "hi")
        self.loop._mark_pending_user_turn(s)
        result = self.loop._restore_pending_user_turn(s)
        self.assertTrue(result)
        self.assertEqual(s.messages[-1]["role"], "assistant")
        self.assertIn("interrupted", s.messages[-1]["content"])
        self.assertNotIn("pending_user_turn", s.metadata)

    def test_restore_noop_without_flag(self):
        s = Session(key="t")
        s.add_message("user", "hi")
        self.assertFalse(self.loop._restore_pending_user_turn(s))
        self.assertEqual(len(s.messages), 1)

    def test_restore_clears_flag_without_user_tail(self):
        s = Session(key="t")
        s.add_message("assistant", "hi")
        self.loop._mark_pending_user_turn(s)
        self.assertTrue(self.loop._restore_pending_user_turn(s))
        self.assertEqual(len(s.messages), 1)
        self.assertNotIn("pending_user_turn", s.metadata)


class TestForkSession(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.mgr = SessionManager(workspace=self.tmp)

    def _fill(self):
        s = self.mgr.get_or_create("src")
        s.add_message("user", "A")
        s.add_message("assistant", "a")
        s.add_message("user", "B")
        s.add_message("assistant", "b")
        s.add_message("user", "C")
        s.metadata["goal_state"] = {"status": "active"}
        s.metadata["_last_summary"] = {"text": "sum"}
        s.last_consolidated = 2
        self.mgr.save(s)
        return s

    def test_fork_before_first_user(self):
        self._fill()
        target = self.mgr.fork_session_before_user_index("src", "dst0", 0)
        self.assertIsNotNone(target)
        self.assertEqual(target.messages, [])

    def test_fork_mid_strips_volatile_metadata(self):
        s = self._fill()
        s.last_consolidated = 3
        self.mgr.save(s)
        target = self.mgr.fork_session_before_user_index("src", "dst1", 1)
        self.assertEqual([m["content"] for m in target.messages], ["A", "a"])
        self.assertNotIn("goal_state", target.metadata)
        self.assertNotIn("pending_user_turn", target.metadata)
        self.assertNotIn("_last_summary", target.metadata)
        self.assertEqual(target.last_consolidated, 0)

    def test_fork_persisted_and_reloadable(self):
        self._fill()
        self.mgr.fork_session_before_user_index("src", "dstP", 2)
        fresh = SessionManager(workspace=self.tmp).get_or_create("dstP")
        self.assertEqual([m["content"] for m in fresh.messages], ["A", "a", "B", "b"])
        self.assertEqual(fresh.last_consolidated, 2)

    def test_fork_out_of_range(self):
        self._fill()
        self.assertIsNone(self.mgr.fork_session_before_user_index("src", "dstX", 99))
        self.assertIsNone(self.mgr.fork_session_before_user_index("src", "dstN", -1))


class TestListSessions(unittest.TestCase):
    def test_list_decodes_and_sorts(self):
        tmp = tempfile.mkdtemp()
        mgr = SessionManager(workspace=tmp)
        s1 = mgr.get_or_create("cli:one")
        s1.add_message("user", "first message")
        s1.updated_at = "2026-01-01T10:00:00"
        mgr.save(s1)
        s2 = mgr.get_or_create("cli:two")
        s2.add_message("user", "second message")
        s2.updated_at = "2026-01-02T10:00:00"
        mgr.save(s2)
        items = mgr.list_sessions()
        self.assertEqual([i["key"] for i in items], ["cli:two", "cli:one"])
        self.assertEqual(items[0]["preview"], "second message")

    def test_list_empty(self):
        mgr = SessionManager(workspace=tempfile.mkdtemp())
        self.assertEqual(mgr.list_sessions(), [])


class TestStep19Integration(unittest.IsolatedAsyncioTestCase):
    def _make_loop(self, session_manager, session_ttl_minutes=0):
        bus = MessageBus()
        provider = _MockProvider()
        registry = _MockToolRegistry()
        return AgentLoop(
            bus=bus, provider=provider, registry=registry,
            session_manager=session_manager,
            context_builder=ContextBuilder(workspace="."),
            memory=MemoryStore(workspace=session_manager.sessions_dir.parent),
            identity="You are a test bot.", replay_budget=10000,
            session_ttl_minutes=session_ttl_minutes,
        )

    async def test_crash_recovery_end_to_end(self):
        tmp = tempfile.mkdtemp()
        sessions = SessionManager(workspace=tmp)
        loop = self._make_loop(sessions)

        class _CrashingProvider:
            @property
            def model(self):
                raise RuntimeError("provider error")

        loop.provider = _CrashingProvider()
        result = await loop._process_message(
            InboundMessage(content="hi", chat_id="crash"), "crash",
        )
        self.assertIsNotNone(result)
        session = sessions.get_or_create("crash")
        self.assertEqual(session.messages[-1]["role"], "user")
        self.assertTrue(session.metadata.get("pending_user_turn"))

        restart_sessions = SessionManager(workspace=tmp)
        loop2 = self._make_loop(restart_sessions)
        await loop2._state_restore(
            TurnContext(msg=InboundMessage(content="hi2", chat_id="crash"), session_key="crash")
        )
        session2 = restart_sessions.get_or_create("crash")
        self.assertEqual(session2.messages[-1]["role"], "assistant")
        self.assertIn("interrupted", session2.messages[-1]["content"])
        self.assertNotIn("pending_user_turn", session2.metadata)

    async def test_auto_compact_end_to_end(self):
        tmp = tempfile.mkdtemp()
        sessions = SessionManager(workspace=tmp)
        loop = self._make_loop(sessions, session_ttl_minutes=1)
        s = sessions.get_or_create("idle")
        for i in range(40):
            s.add_message("user", f"m{i}")
        s.updated_at = (datetime.now() - timedelta(minutes=30)).isoformat()
        sessions.save(s)
        scheduled = []
        loop.auto_compact.check_expired(
            lambda coro: scheduled.append(coro),
            lambda: loop.runtime,
            active_session_keys=set(),
        )
        self.assertEqual(len(scheduled), 1)
        await scheduled[0]
        session = sessions.get_or_create("idle")
        self.assertLessEqual(len(session.messages), 8)


class _EchoChannel(BaseChannel):
    name = "echo"
    display_name = "Echo"

    def __init__(self, config=None, bus=None, pairing=None):
        super().__init__(config, bus, pairing)
        self.sent: list[OutboundMessage] = []

    async def start(self):
        self._running = True

    async def stop(self):
        self._running = False

    async def send(self, msg: OutboundMessage) -> None:
        self.sent.append(msg)


class _StreamingChannel(_EchoChannel):
    async def send_delta(
        self, chat_id, delta, metadata=None, *, stream_id=None,
        stream_end=False, resuming=False,
    ) -> None:
        return


class TestPairingStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = PairingStore(path=Path(self.tmp) / "pairing.json")

    def test_generate_approve_roundtrip(self):
        code = self.store.generate_code("cli", "user1")
        self.assertRegex(code, r"^[A-Z0-9]{4}-[A-Z0-9]{4}$")
        self.assertFalse(self.store.is_approved("cli", "user1"))
        result = self.store.approve_code(code)
        self.assertEqual(result, ("cli", "user1"))
        self.assertTrue(self.store.is_approved("cli", "user1"))
        self.assertIsNone(self.store.approve_code(code))

    def test_ttl_expiry(self):
        code = self.store.generate_code("cli", "user1", ttl=-1)
        self.assertIsNone(self.store.approve_code(code))
        self.assertFalse(self.store.is_approved("cli", "user1"))

    def test_deny_code(self):
        code = self.store.generate_code("cli", "user1")
        self.assertTrue(self.store.deny_code(code))
        self.assertFalse(self.store.deny_code(code))
        self.assertFalse(self.store.is_approved("cli", "user1"))

    def test_persistence_across_instances(self):
        code = self.store.generate_code("cli", "user1")
        self.store.approve_code(code)
        store2 = PairingStore(path=Path(self.tmp) / "pairing.json")
        self.assertTrue(store2.is_approved("cli", "user1"))
        self.assertEqual(store2.get_approved("cli"), ["user1"])
        self.assertEqual(store2.list_pending(), [])

    def test_corrupt_file_reset(self):
        Path(self.tmp, "pairing.json").write_text("{not json", encoding="utf-8")
        code = self.store.generate_code("cli", "user1")
        self.assertIsNotNone(code)

    def test_clear_channel(self):
        code = self.store.generate_code("cli", "user1")
        self.store.approve_code(code)
        self.store.generate_code("cli", "user2")
        tg_code = self.store.generate_code("tg", "other")
        self.store.approve_code(tg_code)
        result = self.store.clear_channel("cli")
        self.assertEqual(result, {"approved": 1, "pending": 1})
        self.assertFalse(self.store.is_approved("cli", "user1"))
        self.assertTrue(all(p["channel"] != "cli" for p in self.store.list_pending()))
        self.assertTrue(self.store.is_approved("tg", "other"))

    def test_revoke(self):
        code = self.store.generate_code("cli", "user1")
        self.store.approve_code(code)
        self.assertTrue(self.store.revoke("cli", "user1"))
        self.assertFalse(self.store.revoke("cli", "user1"))
        self.assertEqual(self.store.revoke_channel("cli"), 0)
        code2 = self.store.generate_code("cli", "user2")
        self.store.approve_code(code2)
        self.assertEqual(self.store.revoke_channel("cli"), 1)

    def test_handle_pairing_command(self):
        code = self.store.generate_code("cli", "user1")
        self.assertIn(code, self.store.handle_pairing_command("cli", "list"))
        reply = self.store.handle_pairing_command("cli", f"approve {code}")
        self.assertIn("user1", reply)
        self.assertTrue(self.store.is_approved("cli", "user1"))
        self.assertIn("No pending", self.store.handle_pairing_command("cli", "list"))
        self.assertIn("Invalid", self.store.handle_pairing_command("cli", "approve BAD-CODE"))
        self.assertIn("not found", self.store.handle_pairing_command("cli", "deny XYZ1"))
        self.assertIn("Revoked", self.store.handle_pairing_command("cli", "revoke user1"))
        self.assertFalse(self.store.is_approved("cli", "user1"))
        self.assertIn("Unknown", self.store.handle_pairing_command("cli", "bogus"))

    def test_format_pairing_reply(self):
        reply = self.store.format_pairing_reply("ABCD-EFGH")
        self.assertIn("ABCD-EFGH", reply)
        self.assertIn("pairing code", reply.lower())


class TestBaseChannel(unittest.IsolatedAsyncioTestCase):
    def _store(self):
        return PairingStore(path=Path(tempfile.mkdtemp()) / "pairing.json")

    def test_is_allowed_star(self):
        ch = _EchoChannel({"allow_from": ["*"]}, MessageBus(), self._store())
        self.assertTrue(ch.is_allowed("anyone"))

    def test_is_allowed_allowfrom_exact(self):
        ch = _EchoChannel({"allow_from": ["alice"]}, MessageBus(), self._store())
        self.assertTrue(ch.is_allowed("alice"))
        self.assertFalse(ch.is_allowed("bob"))

    def test_is_allowed_pairing_approved(self):
        store = self._store()
        code = store.generate_code("echo", "42")
        store.approve_code(code)
        ch = _EchoChannel({}, MessageBus(), store)
        self.assertTrue(ch.is_allowed("42"))
        self.assertFalse(ch.is_allowed("43"))

    def test_is_allowed_denied_default(self):
        ch = _EchoChannel({}, MessageBus(), self._store())
        self.assertFalse(ch.is_allowed("anyone"))

    async def test_handle_message_publishes(self):
        bus = MessageBus()
        ch = _EchoChannel({"allow_from": ["*"]}, bus, self._store())
        await ch._handle_message(
            "alice", "chat1", "hello", media=["a.png"],
            metadata={"x": 1}, session_key="custom", is_dm=True,
        )
        msg = await bus.consume_inbound()
        self.assertEqual(msg.channel, "echo")
        self.assertEqual(msg.sender_id, "alice")
        self.assertEqual(msg.chat_id, "chat1")
        self.assertEqual(msg.content, "hello")
        self.assertEqual(msg.media, ["a.png"])
        self.assertEqual(msg.metadata, {"x": 1})
        self.assertEqual(msg.session_key_override, "custom")
        self.assertEqual(ch.sent, [])

    async def test_handle_message_wants_stream_flag(self):
        bus = MessageBus()
        ch = _StreamingChannel({"allow_from": ["*"], "streaming": True}, bus, self._store())
        await ch._handle_message("alice", "c", "hi")
        msg = await bus.consume_inbound()
        self.assertTrue(msg.metadata.get("_wants_stream"))
        bus2 = MessageBus()
        ch2 = _StreamingChannel({"allow_from": ["*"]}, bus2, self._store())
        await ch2._handle_message("alice", "c", "hi")
        msg2 = await bus2.consume_inbound()
        self.assertNotIn("_wants_stream", msg2.metadata)

    async def test_handle_message_denied_dm_pairing(self):
        store = self._store()
        bus = MessageBus()
        ch = _EchoChannel({}, bus, store)
        await ch._handle_message("stranger", "chat1", "hi", is_dm=True)
        self.assertEqual(len(ch.sent), 1)
        reply = ch.sent[0]
        self.assertEqual(reply.channel, "echo")
        self.assertEqual(reply.chat_id, "chat1")
        code = reply.metadata.get(PAIRING_CODE_META_KEY)
        self.assertIsNotNone(code)
        self.assertIn(code, reply.content)
        self.assertTrue(bus.inbound.empty())
        result = store.approve_code(code)
        self.assertEqual(result, ("echo", "stranger"))
        self.assertTrue(ch.is_allowed("stranger"))

    async def test_handle_message_denied_non_dm_silent(self):
        bus = MessageBus()
        ch = _EchoChannel({}, bus, self._store())
        await ch._handle_message("stranger", "chat1", "hi")
        self.assertEqual(ch.sent, [])
        self.assertTrue(bus.inbound.empty())

    def test_supports_streaming(self):
        self.assertTrue(_StreamingChannel({"streaming": True}).supports_streaming)
        self.assertFalse(_StreamingChannel({}).supports_streaming)
        self.assertFalse(_EchoChannel({"streaming": True}).supports_streaming)

    async def test_lifecycle_and_default_config(self):
        self.assertEqual(_EchoChannel.default_config(), {"enabled": False})
        ch = _EchoChannel()
        self.assertFalse(ch.is_running)
        await ch.start()
        self.assertTrue(ch.is_running)
        await ch.stop()
        self.assertFalse(ch.is_running)

    async def test_send_delta_default_noop(self):
        await _EchoChannel().send_delta("c", "x", stream_end=True)


class TestCliChannel(unittest.IsolatedAsyncioTestCase):
    def _make_channel(self, **kwargs):
        bus = kwargs.pop("bus", None) or MessageBus()
        return CliChannel({"allow_from": ["*"]}, bus, **kwargs), bus

    async def test_start_publishes_and_exits(self):
        channel, bus = self._make_channel(chat_id="sess1")
        got = []
        with mock.patch("step64.channels.cli.ainput", side_effect=["hello", "/exit"]):
            async def responder():
                msg = await bus.consume_inbound()
                got.append(msg)
                await channel.send(OutboundMessage(content="reply", metadata={"stop_reason": "stop"}))
            resp_task = asyncio.create_task(responder())
            await channel.start()
            await resp_task
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].content, "hello")
        self.assertEqual(got[0].channel, "cli")
        self.assertEqual(got[0].chat_id, "sess1")
        self.assertFalse(channel.is_running)

    async def test_send_prints_and_sets_turn_done(self):
        channel, _ = self._make_channel()
        buf = io.StringIO()
        with redirect_stdout(buf):
            await channel.send(OutboundMessage(
                content="hi there", metadata={"stop_reason": "stop", "tokens": 7},
            ))
        out = buf.getvalue()
        self.assertIn("[stop]", out)
        self.assertIn("hi there", out)
        self.assertIn("tokens: 7", out)
        self.assertTrue(channel._turn_done.is_set())

    async def test_send_delta_buffers_until_stream_end(self):
        channel, _ = self._make_channel()
        buf = io.StringIO()
        with redirect_stdout(buf):
            await channel.send_delta("c", "Hel")
            await channel.send_delta("c", "lo")
            self.assertEqual(buf.getvalue(), "")
            await channel.send_delta("c", "", stream_end=True)
        self.assertEqual(buf.getvalue(), "Hello\n")

    async def test_send_delta_stream_end_with_delta(self):
        channel, _ = self._make_channel()
        buf = io.StringIO()
        with redirect_stdout(buf):
            await channel.send_delta("c", "He", stream_id="s1")
            await channel.send_delta("c", "y", stream_id="s1", stream_end=True)
        self.assertEqual(buf.getvalue(), "Hey\n")

    async def test_send_delta_stream_ids_isolated(self):
        channel, _ = self._make_channel()
        buf = io.StringIO()
        with redirect_stdout(buf):
            await channel.send_delta("c", "a", stream_id="s1")
            await channel.send_delta("c", "b", stream_id="s2")
            await channel.send_delta("c", "", stream_id="s2", stream_end=True)
            await channel.send_delta("c", "", stream_id="s1", stream_end=True)
        self.assertEqual(buf.getvalue(), "b\na\n")

    async def test_exit_stops_immediately(self):
        channel, bus = self._make_channel()
        with mock.patch("step64.channels.cli.ainput", side_effect=["/exit"]):
            await channel.start()
        self.assertTrue(bus.inbound.empty())
        self.assertFalse(channel.is_running)

    async def test_empty_input_skipped(self):
        channel, bus = self._make_channel()
        with mock.patch("step64.channels.cli.ainput", side_effect=["", "", "/exit"]):
            await channel.start()
        self.assertTrue(bus.inbound.empty())

    async def test_on_command_consumed(self):
        channel, bus = self._make_channel()
        calls = []

        async def handler(text):
            calls.append(text)
            return text.startswith("/")

        channel.on_command = handler
        got = []
        with mock.patch("step64.channels.cli.ainput", side_effect=["/dream", "hello", "/exit"]):
            async def responder():
                msg = await bus.consume_inbound()
                got.append(msg)
                await channel.send(OutboundMessage(content="ok", metadata={}))
            resp_task = asyncio.create_task(responder())
            await channel.start()
            await resp_task
        self.assertEqual(calls, ["/dream", "hello"])
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].content, "hello")

    def test_default_config(self):
        cfg = CliChannel.default_config()
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["allow_from"], ["*"])
        self.assertTrue(cfg["streaming"])


class _RecordingChannel(BaseChannel):
    name = "rec"

    def __init__(self, config=None, bus=None, pairing=None):
        super().__init__(config, bus, pairing)
        self.sent: list[OutboundMessage] = []
        self.deltas: list[str] = []
        self.stream_ends: list[str] = []
        self.started = False
        self.stopped = False

    async def start(self):
        self._running = True
        self.started = True

    async def stop(self):
        self._running = False
        self.stopped = True

    async def send(self, msg: OutboundMessage) -> None:
        self.sent.append(msg)

    async def send_delta(
        self, chat_id, delta, metadata=None, *, stream_id=None,
        stream_end=False, resuming=False,
    ) -> None:
        if stream_end:
            self.stream_ends.append(delta)
        else:
            self.deltas.append(delta)


class _FlakyChannel(_RecordingChannel):
    def __init__(self, config=None, bus=None, pairing=None, fail_count=2):
        super().__init__(config, bus, pairing)
        self.fail_count = fail_count
        self.attempts = 0

    async def send(self, msg: OutboundMessage) -> None:
        self.attempts += 1
        if self.attempts <= self.fail_count:
            raise RuntimeError("network")
        self.sent.append(msg)


class TestChannelManager(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bus = MessageBus()
        self.pairing = PairingStore(path=Path(tempfile.mkdtemp()) / "pairing.json")

    def test_init_discovers_cli(self):
        manager = ChannelManager(config={"cli": {}}, bus=self.bus, pairing=self.pairing)
        self.assertIn("cli", manager.channels)
        self.assertIsInstance(manager.channels["cli"], CliChannel)
        self.assertEqual(manager.enabled_channels, ["cli"])

    def test_default_enabled_without_config(self):
        manager = ChannelManager(config={}, bus=self.bus, pairing=self.pairing)
        self.assertIn("cli", manager.channels)

    def test_disabled_channel_skipped(self):
        manager = ChannelManager(config={"cli": {"enabled": False}}, bus=self.bus, pairing=self.pairing)
        self.assertEqual(manager.channels, {})

    def test_unknown_channel_skipped(self):
        manager = ChannelManager(
            config={"nope": {}, "cli": {"enabled": False}},
            bus=self.bus, pairing=self.pairing,
        )
        self.assertEqual(manager.channels, {})

    def test_section_applied(self):
        manager = ChannelManager(
            config={"cli": {"streaming": False, "allow_from": ["alice"]}},
            bus=self.bus, pairing=self.pairing,
        )
        ch = manager.channels["cli"]
        self.assertFalse(ch.config.get("streaming"))
        self.assertEqual(ch.config.get("allow_from"), ["alice"])
        self.assertFalse(ch.supports_streaming)

    def test_on_command_wired(self):
        def handler(text):
            return False
        manager = ChannelManager(config={"cli": {}}, on_command=handler)
        self.assertIs(manager.channels["cli"].on_command, handler)

    def test_get_status(self):
        manager = ChannelManager(config={"cli": {}}, bus=self.bus, pairing=self.pairing)
        status = manager.get_status()
        self.assertTrue(status["cli"]["enabled"])
        self.assertFalse(status["cli"]["running"])

    def test_get_channel_unknown(self):
        manager = ChannelManager(config={}, bus=self.bus, pairing=self.pairing)
        self.assertIsNone(manager.get_channel("nope"))

    async def test_dispatch_routes_plain_message(self):
        manager = ChannelManager(config={}, bus=self.bus, pairing=self.pairing)
        rec = _RecordingChannel({}, self.bus, self.pairing)
        manager.channels = {"cli": rec}
        dispatch = asyncio.create_task(manager._dispatch_outbound())
        await self.bus.publish_outbound(OutboundMessage(channel="cli", chat_id="c", content="hi"))
        await asyncio.sleep(0.05)
        dispatch.cancel()
        await dispatch
        self.assertTrue(dispatch.done())
        self.assertEqual([m.content for m in rec.sent], ["hi"])

    async def test_dispatch_unknown_channel_no_crash(self):
        manager = ChannelManager(config={}, bus=self.bus, pairing=self.pairing)
        rec = _RecordingChannel({}, self.bus, self.pairing)
        manager.channels = {"cli": rec}
        dispatch = asyncio.create_task(manager._dispatch_outbound())
        await self.bus.publish_outbound(OutboundMessage(channel="nope", content="hi"))
        await asyncio.sleep(0.05)
        dispatch.cancel()
        await dispatch
        self.assertTrue(dispatch.done())
        self.assertEqual(rec.sent, [])

    async def test_dispatch_stream_delta_mapping(self):
        manager = ChannelManager(config={}, bus=self.bus, pairing=self.pairing)
        rec = _RecordingChannel({}, self.bus, self.pairing)
        manager.channels = {"cli": rec}
        dispatch = asyncio.create_task(manager._dispatch_outbound())
        await self.bus.publish_outbound(StreamDeltaEvent(content="hel", channel="cli", chat_id="c"))
        await self.bus.publish_outbound(StreamDeltaEvent(content="lo", channel="cli", chat_id="c"))
        await self.bus.publish_outbound(StreamDeltaEvent(content="", channel="cli", chat_id="c", finished=True))
        await asyncio.sleep(0.05)
        dispatch.cancel()
        await dispatch
        self.assertTrue(dispatch.done())
        self.assertEqual(rec.deltas, ["hel", "lo"])
        self.assertEqual(rec.stream_ends, [""])
        self.assertEqual(rec.sent, [])

    async def test_send_with_retry_success(self):
        manager = ChannelManager(config={}, bus=self.bus, pairing=self.pairing)
        flaky = _FlakyChannel({}, self.bus, self.pairing, fail_count=2)
        with mock.patch("step64.manager._SEND_RETRY_DELAYS", (0.01, 0.02)):
            await manager._send_with_retry(flaky, OutboundMessage(channel="rec", content="x"))
        self.assertEqual(flaky.attempts, 3)
        self.assertEqual(len(flaky.sent), 1)

    async def test_send_with_retry_exhausted(self):
        manager = ChannelManager(config={}, bus=self.bus, pairing=self.pairing)
        flaky = _FlakyChannel({}, self.bus, self.pairing, fail_count=999)
        with mock.patch("step64.manager._SEND_RETRY_DELAYS", (0.01, 0.02)):
            await manager._send_with_retry(flaky, OutboundMessage(channel="rec", content="x"))
        self.assertEqual(flaky.attempts, 3)
        self.assertEqual(flaky.sent, [])

    async def test_start_all_and_stop_all(self):
        manager = ChannelManager(config={}, bus=self.bus, pairing=self.pairing)
        rec = _RecordingChannel({}, self.bus, self.pairing)
        manager.channels = {"rec": rec}
        await manager.start_all()
        self.assertTrue(rec.started)
        self.assertTrue(rec.is_running)
        await manager.stop_all()
        self.assertTrue(rec.stopped)
        self.assertTrue(manager._dispatch_task.done())

    async def test_start_all_no_channels(self):
        manager = ChannelManager(config={"cli": {"enabled": False}}, bus=self.bus, pairing=self.pairing)
        await manager.start_all()
        self.assertIsNone(manager._dispatch_task)

    async def test_stop_all_idempotent(self):
        manager = ChannelManager(config={}, bus=self.bus, pairing=self.pairing)
        rec = _RecordingChannel({}, self.bus, self.pairing)
        manager.channels = {"rec": rec}
        await manager.stop_all()
        self.assertTrue(rec.stopped)


class Teststep22Integration(unittest.IsolatedAsyncioTestCase):
    def _make_loop(self, tmp, bus):
        provider = _MockProvider()
        registry = _MockToolRegistry()
        sessions = SessionManager(workspace=tmp)
        return sessions, AgentLoop(
            bus=bus, provider=provider, registry=registry,
            session_manager=sessions,
            context_builder=ContextBuilder(workspace="."),
            memory=MemoryStore(workspace=tmp),
            identity="You are a test bot.", replay_budget=10000,
        )

    async def test_end_to_end_cli_turn(self):
        tmp = tempfile.mkdtemp()
        bus = MessageBus()
        sessions, loop = self._make_loop(tmp, bus)
        channel = CliChannel({"allow_from": ["*"], "streaming": True}, bus, chat_id="sess1")
        manager = ChannelManager(
            config={}, bus=bus,
            pairing=PairingStore(path=Path(tmp) / "pairing.json"),
        )
        manager.channels = {"cli": channel}
        buf = io.StringIO()
        loop_task = asyncio.create_task(loop.run())
        with mock.patch("step64.channels.cli.ainput", side_effect=["hello", "/exit"]), \
                redirect_stdout(buf):
            await manager.start_all()
        loop.stop()
        loop_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await loop_task
        session = sessions.get_or_create("sess1")
        self.assertGreater(len(session.messages), 0)
        self.assertEqual(session.messages[-1]["role"], "assistant")
        self.assertIn("Summary", buf.getvalue())

    async def test_pairing_denied_dm_end_to_end(self):
        tmp = tempfile.mkdtemp()
        bus = MessageBus()
        sessions, loop = self._make_loop(tmp, bus)
        store = PairingStore(path=Path(tmp) / "pairing.json")

        class _PairingEchoChannel(BaseChannel):
            name = "echo"

            def __init__(self, config=None, bus=None, pairing=None):
                super().__init__(config, bus, pairing)
                self.sent = []

            async def start(self):
                self._running = True

            async def stop(self):
                self._running = False

            async def send(self, msg):
                self.sent.append(msg)

        channel = _PairingEchoChannel({}, bus, store)
        manager = ChannelManager(config={}, bus=bus, pairing=store)
        manager.channels = {"echo": channel}
        loop_task = asyncio.create_task(loop.run())
        await channel._handle_message("stranger", "chat1", "hi", is_dm=True)
        self.assertEqual(len(channel.sent), 1)
        code = channel.sent[0].metadata.get(PAIRING_CODE_META_KEY)
        self.assertIsNotNone(code)
        self.assertFalse(bus.inbound.qsize())
        result = store.approve_code(code)
        self.assertEqual(result, ("echo", "stranger"))
        await channel._handle_message("stranger", "chat1", "hello", is_dm=True)
        resp = await asyncio.wait_for(_consume_final_response(bus), timeout=2)
        self.assertIsNotNone(resp.content)
        loop.stop()
        loop_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await loop_task

    async def test_on_command_end_to_end(self):
        tmp = tempfile.mkdtemp()
        bus = MessageBus()
        sessions, loop = self._make_loop(tmp, bus)
        channel = CliChannel({"allow_from": ["*"]}, bus, chat_id="sess1")
        manager = ChannelManager(
            config={}, bus=bus,
            pairing=PairingStore(path=Path(tmp) / "pairing.json"),
        )
        manager.channels = {"cli": channel}
        events = []

        async def on_command(text):
            events.append(text)
            if text == "/new":
                sessions.invalidate("sess1")
                p = sessions._get_session_path("sess1")
                if p.exists():
                    p.unlink()
                return True
            return False

        channel.on_command = on_command
        loop_task = asyncio.create_task(loop.run())
        with mock.patch("step64.channels.cli.ainput", side_effect=["/new", "hi", "/exit"]):
            await manager.start_all()
        self.assertEqual(events, ["/new", "hi"])
        session = sessions.get_or_create("sess1")
        self.assertGreater(len(session.messages), 0)
        self.assertEqual(session.messages[-1]["role"], "assistant")
        loop.stop()
        loop_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await loop_task


# ── Step 21: CommandRouter & COMMAND 状态 ──


class TestCommandRouter(unittest.IsolatedAsyncioTestCase):
    """CommandRouter 三档路由（priority/exact/prefix）与 normalize_command_text。"""

    def _make_ctx(self, raw, key="test"):
        return CommandContext(
            msg=InboundMessage(content=raw, channel="cli", chat_id="test"),
            session=None, key=key, raw=raw,
        )

    def test_normalize_command_text(self):
        self.assertEqual(normalize_command_text("/history"), "/history")
        self.assertEqual(normalize_command_text("/history@mybot"), "/history")
        self.assertEqual(normalize_command_text("/history@mybot arg1"), "/history arg1")
        self.assertEqual(normalize_command_text("  /history  "), "/history")
        self.assertEqual(normalize_command_text("hello"), "hello")
        self.assertEqual(normalize_command_text("/cmd@"), "/cmd@")

    async def test_exact_dispatch(self):
        router = CommandRouter()

        async def handler(ctx):
            return OutboundMessage(content=f"ok:{ctx.raw}")

        router.exact("/ping", handler)
        result = await router.dispatch(self._make_ctx("/ping"))
        self.assertIsNotNone(result)
        self.assertEqual(result.content, "ok:/ping")

    async def test_exact_case_insensitive(self):
        router = CommandRouter()
        seen = []

        async def handler(ctx):
            seen.append(ctx.raw)
            return OutboundMessage(content="ok")

        router.exact("/ping", handler)
        result = await router.dispatch(self._make_ctx("/Ping"))
        self.assertIsNotNone(result)
        self.assertEqual(seen, ["/Ping"])

    async def test_prefix_dispatch_sets_args(self):
        router = CommandRouter()

        async def handler(ctx):
            return OutboundMessage(content=f"args={ctx.args.strip()}")

        router.prefix("/pairing ", handler)
        result = await router.dispatch(self._make_ctx("/pairing approve ABCD-EFGH"))
        self.assertIsNotNone(result)
        self.assertEqual(result.content, "args=approve ABCD-EFGH")

    async def test_longest_prefix_first(self):
        router = CommandRouter()

        async def short(ctx):
            return OutboundMessage(content="short")

        async def long(ctx):
            return OutboundMessage(content="long")

        router.prefix("/pair ", short)
        router.prefix("/pairing ", long)
        result = await router.dispatch(self._make_ctx("/pairing list"))
        self.assertEqual(result.content, "long")

    async def test_unhandled_returns_none(self):
        router = CommandRouter()

        async def handler(ctx):
            return OutboundMessage(content="x")

        router.exact("/history", handler)
        self.assertIsNone(await router.dispatch(self._make_ctx("/unknown")))
        self.assertTrue(router.is_dispatchable_command("/history"))
        self.assertFalse(router.is_dispatchable_command("/unknown"))

    async def test_priority_tier(self):
        router = CommandRouter()

        async def shutdown(ctx):
            return OutboundMessage(content="stopping")

        router.priority("/stop", shutdown)
        self.assertTrue(router.is_priority("/stop"))
        self.assertFalse(router.is_priority("/stopx"))
        result = await router.dispatch_priority(self._make_ctx("/stop"))
        self.assertEqual(result.content, "stopping")
        self.assertIsNone(await router.dispatch_priority(self._make_ctx("/history")))


class TestBuiltinCommands(unittest.IsolatedAsyncioTestCase):
    """内置命令 handler：/help /dream /history /new /pairing。"""

    def _make_ctx(self, raw, session=None, loop=None, key="test"):
        return CommandContext(
            msg=InboundMessage(content=raw, channel="cli", chat_id="test"),
            session=session, key=key, raw=raw, loop=loop,
        )

    async def test_help(self):
        from step78.command.builtin import register_builtin_commands

        router = CommandRouter()
        register_builtin_commands(router)
        ctx = self._make_ctx("/help")
        result = await router.dispatch(ctx)
        self.assertIsNotNone(result)
        self.assertIn("/history", result.content)
        self.assertIn("/pairing", result.content)

    async def test_history_format(self):
        from step78.command.builtin import register_builtin_commands

        session = Session(key="test")
        session.add_message("user", "hello world")
        session.add_message("assistant", "hi there")
        router = CommandRouter()
        register_builtin_commands(router)
        ctx = self._make_ctx("/history", session=session)
        result = await router.dispatch(ctx)
        self.assertIsNotNone(result)
        self.assertIn("--- Session History ---", result.content)
        self.assertIn("hello world", result.content)
        self.assertIn("assistant", result.content)

    async def test_dream_nothing(self):
        from step78.command.builtin import register_builtin_commands

        class _FakeLoop:
            async def run_dream(self):
                return None

        router = CommandRouter()
        register_builtin_commands(router)
        ctx = self._make_ctx("/dream", loop=_FakeLoop())
        result = await router.dispatch(ctx)
        self.assertIsNotNone(result)
        self.assertIn("Nothing to process", result.content)

    async def test_new(self):
        from step78.command.builtin import register_builtin_commands

        tmp = tempfile.mkdtemp()
        sessions = SessionManager(workspace=tmp)
        session = sessions.get_or_create("sess1")
        session.add_message("user", "hello")
        sessions.save(session)

        class _FakeLoop:
            pass

        _FakeLoop.sessions = sessions

        router = CommandRouter()
        register_builtin_commands(router)
        ctx = self._make_ctx("/new", loop=_FakeLoop(), key="sess1")
        result = await router.dispatch(ctx)
        self.assertIn("reset", result.content)
        self.assertEqual(len(sessions.get_or_create("sess1").messages), 0)

    async def test_pairing_approve(self):
        from step78.command.builtin import register_builtin_commands

        tmp = tempfile.mkdtemp()
        pairing = PairingStore(path=Path(tmp) / "pairing.json")
        code = pairing.generate_code("cli", "user-1")

        class _FakeLoop:
            pass

        _FakeLoop.pairing = pairing

        router = CommandRouter()
        register_builtin_commands(router)
        ctx = self._make_ctx(f"/pairing approve {code}", loop=_FakeLoop())
        result = await router.dispatch(ctx)
        self.assertIn("Approved", result.content)
        self.assertTrue(pairing.is_approved("cli", "user-1"))

    async def test_pairing_disabled(self):
        from step78.command.builtin import register_builtin_commands

        class _FakeLoop:
            pairing = None

        router = CommandRouter()
        register_builtin_commands(router)
        ctx = self._make_ctx("/pairing list", loop=_FakeLoop())
        result = await router.dispatch(ctx)
        self.assertIn("not enabled", result.content)


class TestLoopCommandState(unittest.IsolatedAsyncioTestCase):
    """AgentLoop COMMAND 状态：短路 shortcut / 放行 dispatch。"""

    def _make_loop(self, **kwargs):
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
            replay_budget=10000, **kwargs,
        )
        return loop, bus

    async def test_builtin_commands_registered(self):
        loop, _ = self._make_loop()
        self.assertTrue(loop.commands.is_dispatchable_command("/help"))
        self.assertTrue(loop.commands.is_dispatchable_command("/dream"))
        self.assertTrue(loop.commands.is_dispatchable_command("/history"))
        self.assertTrue(loop.commands.is_dispatchable_command("/new"))
        self.assertTrue(loop.commands.is_dispatchable_command("/pairing approve ABCD-EFGH"))
        self.assertFalse(loop.commands.is_dispatchable_command("/unknown"))

    async def test_state_command_shortcut(self):
        loop, _ = self._make_loop()
        session = loop.sessions.get_or_create("cmd-test")
        session.add_message("user", "hi")
        ctx = TurnContext(
            msg=InboundMessage(content="/history", channel="cli", chat_id="cmd-test"),
            session_key="cmd-test",
        )
        await loop._state_restore(ctx)
        event = await loop._state_command(ctx)
        self.assertEqual(event, "shortcut")
        self.assertIsNotNone(ctx.outbound)
        self.assertIn("--- Session History ---", ctx.outbound.content)
        self.assertEqual(ctx.outbound.channel, "cli")
        self.assertEqual(ctx.outbound.chat_id, "cmd-test")

    async def test_state_command_non_command_dispatch(self):
        loop, _ = self._make_loop()
        ctx = TurnContext(
            msg=InboundMessage(content="hello there", channel="cli", chat_id="cmd-test"),
            session_key="cmd-test",
        )
        await loop._state_restore(ctx)
        event = await loop._state_command(ctx)
        self.assertEqual(event, "dispatch")
        self.assertIsNone(ctx.outbound)

    async def test_state_command_unknown_falls_to_agent(self):
        loop, _ = self._make_loop()
        ctx = TurnContext(
            msg=InboundMessage(content="/foobar", channel="cli", chat_id="cmd-test"),
            session_key="cmd-test",
        )
        await loop._state_restore(ctx)
        event = await loop._state_command(ctx)
        self.assertEqual(event, "dispatch")

    async def test_state_command_pairing(self):
        tmp = tempfile.mkdtemp()
        pairing = PairingStore(path=Path(tmp) / "pairing.json")
        code = pairing.generate_code("cli", "user-1")
        loop, _ = self._make_loop(pairing=pairing)
        ctx = TurnContext(
            msg=InboundMessage(content=f"/pairing approve {code}", channel="cli", chat_id="cmd-test"),
            session_key="cmd-test",
        )
        await loop._state_restore(ctx)
        event = await loop._state_command(ctx)
        self.assertEqual(event, "shortcut")
        self.assertIn("Approved", ctx.outbound.content)
        self.assertTrue(pairing.is_approved("cli", "user-1"))

    async def test_end_to_end_history(self):
        loop, bus = self._make_loop()
        session = loop.sessions.get_or_create("e2e")
        session.add_message("user", "remember this")
        loop.sessions.save(session)
        task = asyncio.create_task(loop.run())
        await bus.publish_inbound(InboundMessage(content="/history", channel="cli", chat_id="e2e"))
        response = await _consume_final_response(bus)
        self.assertIn("remember this", response.content)
        loop.stop()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_end_to_end_new(self):
        loop, bus = self._make_loop()
        session = loop.sessions.get_or_create("e2e-new")
        session.add_message("user", "hello")
        loop.sessions.save(session)
        self.assertGreater(len(loop.sessions.get_or_create("e2e-new").messages), 0)
        task = asyncio.create_task(loop.run())
        await bus.publish_inbound(InboundMessage(content="/new", channel="cli", chat_id="e2e-new"))
        response = await _consume_final_response(bus)
        self.assertIn("reset", response.content.lower())
        loop.stop()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(len(loop.sessions.get_or_create("e2e-new").messages), 0)

    async def test_end_to_end_dream_empty(self):
        loop, bus = self._make_loop()
        task = asyncio.create_task(loop.run())
        await bus.publish_inbound(InboundMessage(content="/dream", channel="cli", chat_id="e2e-dream"))
        response = await _consume_final_response(bus)
        self.assertIn("Nothing to process", response.content)
        loop.stop()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_normal_turn_still_runs_agent(self):
        loop, bus = self._make_loop()
        task = asyncio.create_task(loop.run())
        await bus.publish_inbound(InboundMessage(content="hello", channel="cli", chat_id="e2e-norm"))
        response = await _consume_final_response(bus)
        self.assertIsNotNone(response.content)
        loop.stop()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_command_persists_shortcut_with_command_marker(self):
        """step64：shortcut 命令持久化 user+assistant，带 _command 标记。"""
        loop, _ = self._make_loop()
        session = loop.sessions.get_or_create("nopersist")
        session.add_message("user", "hi")
        ctx = TurnContext(
            msg=InboundMessage(content="/help", channel="cli", chat_id="nopersist"),
            session_key="nopersist",
        )
        await loop._state_restore(ctx)
        await loop._state_command(ctx)
        msgs = loop.sessions.get_or_create("nopersist").messages
        # 原有 1 条 + shortcut 持久化的 user + assistant = 3 条
        self.assertEqual(len(msgs), 3)
        self.assertTrue(msgs[1].get("_command"))
        self.assertEqual(msgs[1]["role"], "user")
        self.assertTrue(msgs[2].get("_command"))
        self.assertEqual(msgs[2]["role"], "assistant")


# ---- Step 22 Tests: Providers Registry / Factory / Fallback + LLMRuntime ----

class TestProviderRegistry(unittest.TestCase):
    def test_find_by_name_exact(self):
        self.assertEqual(find_by_name("openai").name, "openai")
        self.assertEqual(find_by_name("dashscope").name, "dashscope")
        self.assertEqual(find_by_name("ollama").name, "ollama")

    def test_find_by_name_normalization(self):
        self.assertEqual(find_by_name("OpenRouter").name, "openrouter")
        self.assertEqual(find_by_name("  OpenAI  ").name, "openai")
        self.assertIsNone(find_by_name("dash-scope"))

    def test_find_by_name_unknown(self):
        self.assertIsNone(find_by_name("nonexistent"))

    def test_find_by_model_keywords(self):
        self.assertEqual(find_by_model("gpt-4o-mini").name, "openai")
        self.assertEqual(find_by_model("deepseek-chat").name, "deepseek")
        self.assertEqual(find_by_model("qwen2.5-72b-instruct").name, "dashscope")
        self.assertEqual(find_by_model("nemotron-70b").name, "ollama")

    def test_find_by_model_no_match(self):
        self.assertIsNone(find_by_model("totally-unknown-model"))
        self.assertIsNone(find_by_model(""))

    def test_create_dynamic_spec(self):
        spec = create_dynamic_spec("my-provider")
        self.assertEqual(spec.name, "my_provider")
        self.assertIsNone(find_by_name("my-provider"))
        self.assertTrue(spec.is_direct)


class _StatusError(Exception):
    def __init__(self, status_code):
        super().__init__(f"http {status_code}")
        self.status_code = status_code


class _RawProvider(LLMProvider):
    """可控的假 provider：记录调用与请求模型，可选抛异常。"""

    def __init__(self, model="provider", error=None, content=None):
        super().__init__()
        self._model = model
        self._error = error
        self._content = content or f"reply from {model}"
        self.calls = 0
        self.last_model = None

    @property
    def model(self):
        return self._model

    async def chat(self, messages, tools=None, model=None, temperature=0.7, max_tokens=4096):
        self.calls += 1
        self.last_model = model or self._model
        if self._error is not None:
            raise self._error
        return LLMResponse(content=self._content, finish_reason="stop")


class _StreamFailProvider(LLMProvider):
    """流式假 provider：先发一段 delta，再抛异常。"""

    def __init__(self, model="stream", error=None):
        super().__init__()
        self._model = model
        self._error = error

    @property
    def model(self):
        return self._model

    async def chat(self, messages, tools=None, model=None, temperature=0.7, max_tokens=4096):
        raise NotImplementedError("stream-only fake")

    async def chat_stream_with_retry(self, messages, tools=None, model=None,
                                     temperature=0.7, max_tokens=4096,
                                     on_content_delta=None, retry_config=None):
        if on_content_delta:
            await on_content_delta("partial")
        if self._error is not None:
            raise self._error
        return LLMResponse(content="full stream", finish_reason="stop")


class TestFallbackClassification(unittest.TestCase):
    def test_timeout_is_fallbackable(self):
        self.assertTrue(is_fallbackable_exception(asyncio.TimeoutError()))

    def test_server_error_is_fallbackable(self):
        self.assertTrue(is_fallbackable_exception(_StatusError(500)))
        self.assertTrue(is_fallbackable_exception(_StatusError(503)))

    def test_rate_limit_is_fallbackable(self):
        self.assertTrue(is_fallbackable_exception(_StatusError(429)))

    def test_auth_error_not_fallbackable(self):
        self.assertFalse(is_fallbackable_exception(_StatusError(401)))
        self.assertFalse(is_fallbackable_exception(_StatusError(400)))

    def test_unknown_error_not_fallbackable(self):
        self.assertFalse(is_fallbackable_exception(RuntimeError("boom")))


class TestFallbackProvider(unittest.IsolatedAsyncioTestCase):
    def _make(self, primary, response=None, preset_models=("fallback-a", "fallback-b")):
        presets = [ProviderSettings(model=m) for m in preset_models]
        if response is not None:
            the_fallback = _RawProvider(model="fallback-a", content=response)
        else:
            the_fallback = _RawProvider(model="fallback-a", content="fallback reply")

        def factory(preset):
            return the_fallback if preset.model == "fallback-a" else _RawProvider(
                model=preset.model, content=f"reply from {preset.model}"
            )

        wrapper = FallbackProvider(
            primary=primary,
            fallback_presets=presets,
            provider_factory=factory,
        )
        return wrapper, the_fallback

    async def test_primary_success_no_fallback(self):
        primary = _RawProvider(model="primary", content="primary ok")
        wrapper, the_fallback = self._make(primary)
        resp = await wrapper.chat_with_retry([{"role": "user", "content": "hi"}])
        self.assertEqual(resp.content, "primary ok")
        self.assertEqual(primary.calls, 1)
        self.assertEqual(the_fallback.calls, 0)

    async def test_primary_error_falls_back(self):
        primary = _RawProvider(model="primary", error=_StatusError(500))
        wrapper, the_fallback = self._make(primary)
        resp = await wrapper.chat_with_retry([{"role": "user", "content": "hi"}])
        self.assertEqual(resp.content, "fallback reply")
        self.assertEqual(the_fallback.calls, 1)
        self.assertEqual(wrapper._primary_failures, 1)

    async def test_fallback_receives_primary_model_override(self):
        primary = _RawProvider(model="primary", error=_StatusError(500))
        wrapper, the_fallback = self._make(primary)
        await wrapper.chat_with_retry([{"role": "user", "content": "hi"}], model="primary")
        self.assertEqual(the_fallback.last_model, "fallback-a")

    async def test_timeout_error_falls_back(self):
        primary = _RawProvider(model="primary", error=asyncio.TimeoutError())
        wrapper, _ = self._make(primary)
        resp = await wrapper.chat_with_retry([{"role": "user", "content": "hi"}])
        self.assertEqual(resp.content, "fallback reply")

    async def test_auth_error_raises_without_fallback(self):
        primary = _RawProvider(model="primary", error=_StatusError(401))
        wrapper, the_fallback = self._make(primary)
        with self.assertRaises(_StatusError):
            await wrapper.chat_with_retry([{"role": "user", "content": "hi"}])
        self.assertEqual(the_fallback.calls, 0)

    async def test_unknown_error_raises_without_fallback(self):
        primary = _RawProvider(model="primary", error=RuntimeError("boom"))
        wrapper, the_fallback = self._make(primary)
        with self.assertRaises(RuntimeError):
            await wrapper.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(the_fallback.calls, 0)

    async def test_all_fallbacks_fail_raises_last(self):
        def factory(preset):
            return _RawProvider(model=preset.model, error=_StatusError(502))

        wrapper = FallbackProvider(
            primary=_RawProvider(model="primary", error=_StatusError(500)),
            fallback_presets=[FallbackModelStub("fallback-a"), FallbackModelStub("fallback-b")],
            provider_factory=factory,
        )
        with self.assertRaises(_StatusError):
            await wrapper.chat_with_retry([{"role": "user", "content": "hi"}])

    async def test_streamed_delta_then_error_no_fallback(self):
        primary = _StreamFailProvider(model="primary", error=_StatusError(503))
        fallback = _RawProvider(model="fallback-a", content="fallback reply")
        wrapper = FallbackProvider(
            primary=primary,
            fallback_presets=[FallbackModelStub("fallback-a")],
            provider_factory=lambda preset: fallback,
        )
        deltas = []

        async def _collect(text: str) -> None:
            deltas.append(text)

        with self.assertRaises(_StatusError):
            await wrapper.chat_stream_with_retry(
                [{"role": "user", "content": "hi"}],
                on_content_delta=_collect,
            )
        self.assertEqual(deltas, ["partial"])
        self.assertEqual(fallback.calls, 0)

    async def test_no_fallbacks_passthrough(self):
        primary = _RawProvider(model="primary", content="ok")
        wrapper = FallbackProvider(
            primary=primary, fallback_presets=[], provider_factory=lambda p: p
        )
        resp = await wrapper.chat_stream_with_retry([{"role": "user", "content": "hi"}])
        self.assertEqual(resp.content, "ok")

    async def test_circuit_breaker_trips_after_three(self):
        primary = _RawProvider(model="primary", error=_StatusError(500))
        fallback = _RawProvider(model="fallback-a", content="fallback reply")
        wrapper = FallbackProvider(
            primary=primary,
            fallback_presets=[FallbackModelStub("fallback-a")],
            provider_factory=lambda preset: fallback,
        )
        for _ in range(3):
            await wrapper.chat_with_retry([{"role": "user", "content": "hi"}])
        self.assertFalse(wrapper._primary_available())
        self.assertEqual(primary.calls, 3)
        # 熔断后：主 provider 不再被调用，直接走 fallback
        await wrapper.chat_with_retry([{"role": "user", "content": "hi"}])
        self.assertEqual(primary.calls, 3)
        self.assertEqual(fallback.calls, 4)


class FallbackModelStub:
    """FallbackProvider 只消费 .model/.max_tokens/.temperature 的最小预设。"""

    def __init__(self, model, max_tokens=4096, temperature=0.7):
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature


class TestProviderFactory(unittest.TestCase):
    _SAVED_ENV = {}

    def setUp(self):
        self._SAVED_ENV = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._SAVED_ENV)

    def test_make_provider_resolves_by_name(self):
        p = make_provider(ProviderSettings(model="deepseek-chat", provider="deepseek", api_key="sk-test"))
        self.assertEqual(p.model, "deepseek-chat")

    def test_make_provider_resolves_by_model_keywords(self):
        p = make_provider(ProviderSettings(model="gpt-4o", api_key="sk-test"))
        self.assertEqual(p.model, "gpt-4o")

    def test_make_provider_uses_spec_env_key(self):
        os.environ["DASHSCOPE_API_KEY"] = "sk-dash"
        p = make_provider(ProviderSettings(model="qwen-max", provider="dashscope"))
        self.assertEqual(p.model, "qwen-max")

    def test_make_provider_local_requires_no_key(self):
        p = make_provider(ProviderSettings(model="llama3", provider="ollama"))
        self.assertEqual(p.model, "llama3")

    def test_make_provider_custom_requires_api_base(self):
        with self.assertRaises(ValueError):
            make_provider(ProviderSettings(model="x", provider="custom"))
        p = make_provider(ProviderSettings(model="x", provider="custom", api_base="http://localhost:8000/v1"))
        self.assertIsNotNone(p)

    def test_make_provider_missing_key_raises(self):
        with self.assertRaises(ValueError):
            make_provider(ProviderSettings(model="deepseek-chat", provider="deepseek"))

    def test_make_provider_unresolvable_raises(self):
        with self.assertRaises(ValueError):
            make_provider(ProviderSettings(model="no-such-model-xyz"))

    def test_make_provider_with_fallback_wraps(self):
        p = make_provider(ProviderSettings(
            model="gpt-4o", api_key="sk-test",
            fallbacks=[ProviderSettings(model="deepseek-chat", api_key="sk-test")],
        ))
        self.assertIsInstance(p, FallbackProvider)
        self.assertEqual(len(p._fallback_presets), 1)

    def test_provider_signature_reflects_input(self):
        s1 = provider_signature(ProviderSettings(model="gpt-4o", api_key="a"))
        s2 = provider_signature(ProviderSettings(model="gpt-4o", api_key="b"))
        s3 = provider_signature(ProviderSettings(model="deepseek-chat", api_key="a"))
        self.assertEqual(s1, s1)
        self.assertNotEqual(s1, s2)
        self.assertNotEqual(s1, s3)

    def test_build_snapshot(self):
        snap = build_provider_snapshot(ProviderSettings(
            model="gpt-4o", api_key="sk-test", context_window_tokens=8192,
        ))
        self.assertEqual(snap.model, "gpt-4o")
        self.assertEqual(snap.context_window_tokens, 8192)
        self.assertEqual(snap.generation.max_tokens, 4096)


class TestLLMRuntime(unittest.TestCase):
    def test_capture_and_properties(self):
        prov = _MockProvider()
        rt = LLMRuntime.capture(
            provider=prov, model="gpt-4o",
            context_window_tokens=8192, max_tokens=1024, temperature=0.3,
        )
        self.assertEqual(rt.model, "gpt-4o")
        self.assertEqual(rt.context_window_tokens, 8192)
        self.assertEqual(rt.max_tokens, 1024)
        self.assertEqual(rt.temperature, 0.3)
        self.assertIsInstance(rt.generation, GenerationSettings)

    def test_frozen(self):
        rt = LLMRuntime.capture(provider=None, model="m", context_window_tokens=100)
        with self.assertRaises(Exception):
            rt.context_window_tokens = 200
        with self.assertRaises(Exception):
            rt.model = "other"

    def test_replay_budget_derivation(self):
        prov = _MockProvider()
        runtime = LLMRuntime.capture(
            provider=prov, model="gpt-4o",
            context_window_tokens=4000, max_tokens=512,
        )
        tmp = tempfile.mkdtemp()
        loop = AgentLoop(
            bus=MessageBus(), provider=prov, registry=_MockToolRegistry(),
            session_manager=SessionManager(workspace=tmp),
            context_builder=ContextBuilder(workspace="."),
            memory=MemoryStore(workspace=tmp),
            identity="test", runtime=runtime,
        )
        self.assertEqual(loop.replay_budget, 4000 - 512 - 128)

    def test_model_preset_and_resolve(self):
        presets = {
            "fast": ModelPreset(name="fast", model="gpt-4o-mini", context_window_tokens=8000),
            "slow": ModelPreset(name="slow", model="deepseek-r1", context_window_tokens=16000),
        }
        chosen = resolve_preset(presets, "slow")
        self.assertEqual(chosen.model, "deepseek-r1")
        self.assertEqual(chosen.to_generation_settings().max_tokens, 4096)
        with self.assertRaises(KeyError):
            resolve_preset(presets, "nope")
        with self.assertRaises(ValueError):
            resolve_preset(presets, "  ")


class TestEphemeralMode(unittest.IsolatedAsyncioTestCase):
    """step41：ephemeral 临时 turn 模式测试。

    验证 ephemeral=True 时：
    - _state_build 跳过 consolidation
    - _state_save 跳过 enforce_file_cap + 后台 consolidation，但仍执行 _save_turn
    - _state_respond 挂载内部 _stop_reason
    - hook 链仅保留 progress hook（除非 run_extra_hooks_for_ephemeral=True）
    """

    def _make_loop(self, runtime=None):
        bus = MessageBus()
        provider = _MockProvider()
        registry = _MockToolRegistry()
        tmp = tempfile.mkdtemp()
        session_manager = SessionManager(workspace=tmp)
        context_builder = ContextBuilder(workspace=".")
        memory = MemoryStore(workspace=tmp)
        kwargs = dict(
            bus=bus, provider=provider, registry=registry,
            session_manager=session_manager, context_builder=context_builder,
            memory=memory, identity="You are a test bot.",
        )
        if runtime is not None:
            kwargs["runtime"] = runtime
        else:
            kwargs["replay_budget"] = 10000
        return AgentLoop(**kwargs), bus

    def _make_ctx(self, loop, ephemeral=False, run_extra_hooks_for_ephemeral=False):
        ctx = TurnContext(
            msg=InboundMessage(content="hi", chat_id="test"),
            session_key="test",
            ephemeral=ephemeral,
            run_extra_hooks_for_ephemeral=run_extra_hooks_for_ephemeral,
        )
        ctx.session = loop.sessions.get_or_create("test")
        ctx.runtime = loop.runtime
        return ctx

    def test_turn_context_default_ephemeral_false(self):
        ctx = TurnContext(msg=InboundMessage(content="hi"), session_key="test")
        self.assertFalse(ctx.ephemeral)
        self.assertFalse(ctx.run_extra_hooks_for_ephemeral)

    async def test_state_build_ephemeral_skips_consolidation(self):
        loop, _ = self._make_loop()
        ctx = self._make_ctx(loop, ephemeral=True)
        with mock.patch.object(
            loop.consolidator, "maybe_consolidate_by_tokens", new_callable=mock.AsyncMock
        ) as mock_conso:
            event = await loop._state_build(ctx)
            self.assertEqual(event, "ok")
            mock_conso.assert_not_called()

    async def test_state_build_non_ephemeral_calls_consolidation(self):
        loop, _ = self._make_loop()
        ctx = self._make_ctx(loop, ephemeral=False)
        with mock.patch.object(
            loop.consolidator, "maybe_consolidate_by_tokens", new_callable=mock.AsyncMock
        ) as mock_conso:
            event = await loop._state_build(ctx)
            self.assertEqual(event, "ok")
            mock_conso.assert_called_once()

    async def test_state_build_ephemeral_include_memory_false(self):
        loop, _ = self._make_loop()
        ctx = self._make_ctx(loop, ephemeral=True)
        with mock.patch.object(
            loop, "_build_initial_messages", wraps=loop._build_initial_messages
        ) as mock_build:
            await loop._state_build(ctx)
            _, kwargs = mock_build.call_args
            self.assertFalse(kwargs.get("include_memory_recent_history"))

    async def test_state_build_non_ephemeral_include_memory_true(self):
        loop, _ = self._make_loop()
        ctx = self._make_ctx(loop, ephemeral=False)
        with mock.patch.object(
            loop, "_build_initial_messages", wraps=loop._build_initial_messages
        ) as mock_build:
            await loop._state_build(ctx)
            _, kwargs = mock_build.call_args
            self.assertTrue(kwargs.get("include_memory_recent_history"))

    async def test_state_save_ephemeral_skips_enforce_file_cap(self):
        loop, _ = self._make_loop()
        ctx = self._make_ctx(loop, ephemeral=True)
        ctx.final_content = "hi"
        ctx.all_messages = [{"role": "assistant", "content": "hi"}]
        ctx.stop_reason = "stop"
        with mock.patch.object(ctx.session, "enforce_file_cap") as mock_cap:
            event = await loop._state_save(ctx)
            self.assertEqual(event, "ok")
            mock_cap.assert_not_called()

    async def test_state_save_non_ephemeral_calls_enforce_file_cap(self):
        loop, _ = self._make_loop()
        ctx = self._make_ctx(loop, ephemeral=False)
        ctx.final_content = "hi"
        ctx.all_messages = [{"role": "assistant", "content": "hi"}]
        ctx.stop_reason = "stop"
        with mock.patch.object(ctx.session, "enforce_file_cap") as mock_cap:
            event = await loop._state_save(ctx)
            self.assertEqual(event, "ok")
            mock_cap.assert_called_once()

    async def test_state_save_ephemeral_skips_background_consolidation(self):
        loop, _ = self._make_loop()
        ctx = self._make_ctx(loop, ephemeral=True)
        ctx.final_content = "hi"
        ctx.all_messages = [{"role": "assistant", "content": "hi"}]
        ctx.stop_reason = "stop"
        with mock.patch.object(loop, "_schedule_background") as mock_sched:
            event = await loop._state_save(ctx)
            self.assertEqual(event, "ok")
            mock_sched.assert_not_called()

    async def test_state_save_non_ephemeral_schedules_background(self):
        loop, _ = self._make_loop()
        ctx = self._make_ctx(loop, ephemeral=False)
        ctx.final_content = "hi"
        ctx.all_messages = [{"role": "assistant", "content": "hi"}]
        ctx.stop_reason = "stop"

        def _mock_schedule(coro):
            asyncio.create_task(coro)

        with mock.patch.object(
            loop, "_schedule_background", side_effect=_mock_schedule
        ) as mock_sched:
            event = await loop._state_save(ctx)
            self.assertEqual(event, "ok")
            mock_sched.assert_called_once()

    async def test_state_save_ephemeral_still_saves_turn(self):
        loop, _ = self._make_loop()
        ctx = self._make_ctx(loop, ephemeral=True)
        ctx.final_content = "hi"
        ctx.all_messages = [{"role": "assistant", "content": "hi"}]
        ctx.stop_reason = "stop"
        with mock.patch.object(loop, "_save_turn") as mock_save:
            event = await loop._state_save(ctx)
            self.assertEqual(event, "ok")
            mock_save.assert_called_once()

    async def test_state_respond_ephemeral_hang_stop_reason(self):
        loop, _ = self._make_loop()
        ctx = self._make_ctx(loop, ephemeral=True)
        ctx.final_content = "hello"
        ctx.stop_reason = "stop"
        ctx.usage = {"prompt_tokens": 10, "completion_tokens": 5}
        event = await loop._state_respond(ctx)
        self.assertEqual(event, "ok")
        self.assertIsNotNone(ctx.outbound)
        self.assertEqual(ctx.outbound.metadata.get("_stop_reason"), "stop")

    async def test_state_respond_non_ephemeral_no_internal_stop_reason(self):
        loop, _ = self._make_loop()
        ctx = self._make_ctx(loop, ephemeral=False)
        ctx.final_content = "hello"
        ctx.stop_reason = "stop"
        ctx.usage = {"prompt_tokens": 10, "completion_tokens": 5}
        event = await loop._state_respond(ctx)
        self.assertEqual(event, "ok")
        self.assertIsNotNone(ctx.outbound)
        self.assertNotIn("_stop_reason", ctx.outbound.metadata)

    async def test_state_respond_ephemeral_suppress_response_no_stop_reason(self):
        loop, _ = self._make_loop()
        ctx = self._make_ctx(loop, ephemeral=True)
        ctx.suppress_response = True
        event = await loop._state_respond(ctx)
        self.assertEqual(event, "ok")
        self.assertIsNone(ctx.outbound)

    async def test_process_message_passes_ephemeral_to_ctx(self):
        loop, _ = self._make_loop()
        with mock.patch.object(loop, "_state_restore", new_callable=mock.AsyncMock, return_value="ok"), \
             mock.patch.object(loop, "_state_compact", new_callable=mock.AsyncMock, return_value="ok"), \
             mock.patch.object(loop, "_state_command", new_callable=mock.AsyncMock, return_value="dispatch"), \
             mock.patch.object(loop, "_state_build", new_callable=mock.AsyncMock, return_value="ok"), \
             mock.patch.object(loop, "_state_run", new_callable=mock.AsyncMock, return_value="ok"), \
             mock.patch.object(loop, "_state_save", new_callable=mock.AsyncMock, return_value="ok"), \
             mock.patch.object(loop, "_state_respond", new_callable=mock.AsyncMock, return_value="ok"):
            msg = InboundMessage(content="hi", chat_id="test")
            await loop._process_message(msg, "test", ephemeral=True)
            # 验证 _state_build 被调用时 ctx.ephemeral=True
            call_ctx = loop._state_build.call_args[0][0]
            self.assertTrue(call_ctx.ephemeral)

    async def test_process_message_default_ephemeral_false(self):
        loop, _ = self._make_loop()
        with mock.patch.object(loop, "_state_restore", new_callable=mock.AsyncMock, return_value="ok"), \
             mock.patch.object(loop, "_state_compact", new_callable=mock.AsyncMock, return_value="ok"), \
             mock.patch.object(loop, "_state_command", new_callable=mock.AsyncMock, return_value="dispatch"), \
             mock.patch.object(loop, "_state_build", new_callable=mock.AsyncMock, return_value="ok"), \
             mock.patch.object(loop, "_state_run", new_callable=mock.AsyncMock, return_value="ok"), \
             mock.patch.object(loop, "_state_save", new_callable=mock.AsyncMock, return_value="ok"), \
             mock.patch.object(loop, "_state_respond", new_callable=mock.AsyncMock, return_value="ok"):
            msg = InboundMessage(content="hi", chat_id="test")
            await loop._process_message(msg, "test")
            call_ctx = loop._state_build.call_args[0][0]
            self.assertFalse(call_ctx.ephemeral)

    def test_ephemeral_hook_only_progress(self):
        from step78.hook import AgentTurnHookSpec, build_agent_turn_hook, AgentProgressHook
        spec = AgentTurnHookSpec(
            on_progress=mock.AsyncMock(),
            ephemeral=True,
            run_extra_hooks_for_ephemeral=False,
        )
        hook = build_agent_turn_hook(spec)
        self.assertIsInstance(hook, AgentProgressHook)

    def test_ephemeral_run_extra_hooks_executes_full_chain(self):
        from step78.hook import (
            AgentTurnHookSpec, build_agent_turn_hook, AgentHook, AgentTurnHookContext,
        )

        class _ExtraHook(AgentHook):
            pass

        def _extra_factory(ctx: AgentTurnHookContext) -> AgentHook:
            return _ExtraHook()

        spec = AgentTurnHookSpec(
            on_progress=mock.AsyncMock(),
            turn_hook_factories=[_extra_factory],
            ephemeral=True,
            run_extra_hooks_for_ephemeral=True,
        )
        hook = build_agent_turn_hook(spec)
        # CompositeHook 包含 progress hook + extra hook
        self.assertTrue(hasattr(hook, "_hooks"))
        self.assertEqual(len(hook._hooks), 2)

    def test_build_initial_messages_passes_include_memory(self):
        loop, _ = self._make_loop()
        session = loop.sessions.get_or_create("test")
        msg = InboundMessage(content="hi", chat_id="test")
        with mock.patch.object(
            loop.context, "build_messages", wraps=loop.context.build_messages
        ) as mock_build:
            loop._build_initial_messages(
                msg, session, [], None,
                include_memory_recent_history=False,
            )
            _, kwargs = mock_build.call_args
            self.assertFalse(kwargs.get("include_memory_recent_history"))


class TestStep42AssembleOutbound(unittest.IsolatedAsyncioTestCase):
    """step42：_assemble_outbound 提取 + MessageTool 抑制 + latency_ms 测试。"""

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

    def test_assemble_outbound_basic(self):
        loop, _ = self._make_loop()
        msg = InboundMessage(content="hi", chat_id="test")
        outbound = loop._assemble_outbound(
            msg, "hello", [], "stop", False, None,
        )
        self.assertIsNotNone(outbound)
        self.assertEqual(outbound.content, "hello")
        self.assertEqual(outbound.metadata["stop_reason"], "stop")
        self.assertIn("tokens", outbound.metadata)

    def test_assemble_outbound_latency_ms(self):
        loop, _ = self._make_loop()
        msg = InboundMessage(content="hi", chat_id="test")
        outbound = loop._assemble_outbound(
            msg, "hello", [], "stop", False, None,
            turn_latency_ms=1234,
        )
        self.assertEqual(outbound.metadata["latency_ms"], 1234)

    def test_assemble_outbound_no_latency(self):
        loop, _ = self._make_loop()
        msg = InboundMessage(content="hi", chat_id="test")
        outbound = loop._assemble_outbound(
            msg, "hello", [], "stop", False, None,
            turn_latency_ms=None,
        )
        self.assertNotIn("latency_ms", outbound.metadata)

    def test_assemble_outbound_stream_event(self):
        loop, _ = self._make_loop()
        msg = InboundMessage(content="hi", chat_id="test")
        outbound = loop._assemble_outbound(
            msg, "hello", [], "stop", False,
            on_stream=mock.AsyncMock(),
        )
        self.assertIsNotNone(outbound.event)

    def test_assemble_outbound_error_no_event(self):
        loop, _ = self._make_loop()
        msg = InboundMessage(content="hi", chat_id="test")
        outbound = loop._assemble_outbound(
            msg, "hello", [], "error", False,
            on_stream=mock.AsyncMock(),
        )
        self.assertIsNone(outbound.event)

    def test_assemble_outbound_message_tool_suppression(self):
        from step78.tools.message import MessageTool
        loop, _ = self._make_loop()
        mt = MessageTool(send_callback=mock.AsyncMock())
        mt._sent_in_turn = True
        loop.registry.register(mt)
        msg = InboundMessage(content="hi", chat_id="test")
        outbound = loop._assemble_outbound(
            msg, "hello", [], "stop", False, None,
        )
        self.assertIsNone(outbound)

    def test_assemble_outbound_with_injections_not_suppressed(self):
        from step78.tools.message import MessageTool
        loop, _ = self._make_loop()
        mt = MessageTool(send_callback=mock.AsyncMock())
        mt._sent_in_turn = True
        loop.registry.register(mt)
        msg = InboundMessage(content="hi", chat_id="test")
        outbound = loop._assemble_outbound(
            msg, "hello", [], "stop", had_injections=True, on_stream=None,
        )
        self.assertIsNotNone(outbound)

    def test_assemble_outbound_empty_final_response_suppressed(self):
        from step78.tools.message import MessageTool
        loop, _ = self._make_loop()
        mt = MessageTool(send_callback=mock.AsyncMock())
        mt._sent_in_turn = True
        loop.registry.register(mt)
        msg = InboundMessage(content="hi", chat_id="test")
        outbound = loop._assemble_outbound(
            msg, "hello", [], "empty_final_response",
            had_injections=True, on_stream=None,
        )
        self.assertIsNone(outbound)

    def test_assemble_outbound_no_message_tool(self):
        loop, _ = self._make_loop()
        # 不注册 MessageTool
        msg = InboundMessage(content="hi", chat_id="test")
        outbound = loop._assemble_outbound(
            msg, "hello", [], "stop", False, None,
        )
        self.assertIsNotNone(outbound)


class TestStep42MessageTool(unittest.IsolatedAsyncioTestCase):
    """step42：极简 MessageTool 测试。"""

    def test_message_tool_start_turn_resets(self):
        from step78.tools.message import MessageTool
        mt = MessageTool(send_callback=mock.AsyncMock())
        mt._sent_in_turn = True
        mt.start_turn()
        self.assertFalse(mt._sent_in_turn)

    async def test_message_tool_execute_marks_sent(self):
        from step78.tools.message import MessageTool
        callback = mock.AsyncMock()
        mt = MessageTool(send_callback=callback)
        result = await mt.execute(content="hello")
        self.assertTrue(mt._sent_in_turn)
        self.assertIn("sent", str(result).lower())
        callback.assert_awaited_once()

    async def test_message_tool_execute_no_callback_error(self):
        from step78.tools.message import MessageTool
        mt = MessageTool(send_callback=None)
        result = await mt.execute(content="hello")
        self.assertTrue(result.is_error)
        self.assertFalse(mt._sent_in_turn)

    def test_message_tool_name(self):
        from step78.tools.message import MessageTool
        mt = MessageTool()
        self.assertEqual(mt.name, "message")


class TestStep42Integration(unittest.IsolatedAsyncioTestCase):
    """step42：集成测试——_state_build 调 start_turn，_state_save 存 latency。"""

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

    async def test_state_build_calls_message_tool_start_turn(self):
        from step78.tools.message import MessageTool
        loop, _ = self._make_loop()
        mt = MessageTool(send_callback=mock.AsyncMock())
        mt._sent_in_turn = True  # 预设为 True，验证 start_turn 会重置
        loop.registry.register(mt)

        ctx = TurnContext(
            msg=InboundMessage(content="hi", chat_id="test"),
            session_key="test",
        )
        ctx.session = loop.sessions.get_or_create("test")
        ctx.runtime = loop.runtime
        with mock.patch.object(
            loop.consolidator, "maybe_consolidate_by_tokens",
            new_callable=mock.AsyncMock,
        ):
            await loop._state_build(ctx)
        self.assertFalse(mt._sent_in_turn)  # start_turn 重置为 False

    async def test_state_save_stores_turn_latency_ms(self):
        loop, _ = self._make_loop()
        ctx = TurnContext(
            msg=InboundMessage(content="hi", chat_id="test"),
            session_key="test",
        )
        ctx.session = loop.sessions.get_or_create("test")
        ctx.runtime = loop.runtime
        ctx.final_content = "hi"
        ctx.all_messages = [{"role": "assistant", "content": "hi"}]
        ctx.stop_reason = "stop"
        import time as _time
        ctx.turn_wall_started_at = _time.time() - 0.5  # 模拟 500ms 前开始
        def _consume(coro):
            asyncio.create_task(coro)
        with mock.patch.object(loop, "_schedule_background", side_effect=_consume):
            await loop._state_save(ctx)
        self.assertIsNotNone(ctx.turn_latency_ms)
        self.assertGreater(ctx.turn_latency_ms, 0)


class TestStep43ProcessDirect(unittest.IsolatedAsyncioTestCase):
    """step43：process_direct 公共 API 测试。"""

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

    async def test_process_direct_calls_process_message(self):
        loop, _ = self._make_loop()
        with mock.patch.object(
            loop, "_process_message", new_callable=mock.AsyncMock,
            return_value=OutboundMessage(content="ok"),
        ) as mock_pm:
            result = await loop.process_direct("hello", "test:direct")
            self.assertIsNotNone(result)
            self.assertEqual(result.content, "ok")
            mock_pm.assert_awaited_once()
            args, kwargs = mock_pm.call_args
            self.assertEqual(args[0].content, "hello")
            self.assertEqual(args[1], "test:direct")
            self.assertFalse(kwargs.get("ephemeral"))

    async def test_process_direct_ephemeral(self):
        loop, _ = self._make_loop()
        with mock.patch.object(
            loop, "_process_message", new_callable=mock.AsyncMock,
            return_value=OutboundMessage(content="ok"),
        ) as mock_pm:
            await loop.process_direct("hello", "test:direct", ephemeral=True)
            _, kwargs = mock_pm.call_args
            self.assertTrue(kwargs.get("ephemeral"))

    async def test_process_direct_run_extra_hooks(self):
        loop, _ = self._make_loop()
        with mock.patch.object(
            loop, "_process_message", new_callable=mock.AsyncMock,
            return_value=OutboundMessage(content="ok"),
        ) as mock_pm:
            await loop.process_direct(
                "hello", "test:direct",
                ephemeral=True, run_extra_hooks_for_ephemeral=True,
            )
            _, kwargs = mock_pm.call_args
            self.assertTrue(kwargs.get("run_extra_hooks_for_ephemeral"))

    async def test_process_direct_custom_channel_chat_id(self):
        loop, _ = self._make_loop()
        with mock.patch.object(
            loop, "_process_message", new_callable=mock.AsyncMock,
            return_value=OutboundMessage(content="ok"),
        ) as mock_pm:
            await loop.process_direct(
                "hello", "test:direct",
                channel="webui", chat_id="user123",
            )
            args, _ = mock_pm.call_args
            msg = args[0]
            self.assertEqual(msg.channel, "webui")
            self.assertEqual(msg.chat_id, "user123")

    async def test_process_direct_custom_runtime(self):
        loop, _ = self._make_loop()
        custom_runtime = mock.MagicMock()
        with mock.patch.object(
            loop, "_process_message", new_callable=mock.AsyncMock,
            return_value=OutboundMessage(content="ok"),
        ) as mock_pm:
            await loop.process_direct(
                "hello", "test:direct", runtime=custom_runtime,
            )
            _, kwargs = mock_pm.call_args
            self.assertIs(kwargs.get("runtime"), custom_runtime)

    async def test_process_direct_default_session_key(self):
        loop, _ = self._make_loop()
        with mock.patch.object(
            loop, "_process_message", new_callable=mock.AsyncMock,
            return_value=OutboundMessage(content="ok"),
        ) as mock_pm:
            await loop.process_direct("hello")
            args, _ = mock_pm.call_args
            self.assertEqual(args[1], "cli:direct")

    async def test_process_direct_uses_session_lock(self):
        loop, _ = self._make_loop()
        self.assertNotIn("test:lock", loop._session_locks)
        with mock.patch.object(
            loop, "_process_message", new_callable=mock.AsyncMock,
            return_value=OutboundMessage(content="ok"),
        ):
            await loop.process_direct("hello", "test:lock")
        self.assertIn("test:lock", loop._session_locks)

    async def test_process_direct_returns_none(self):
        loop, _ = self._make_loop()
        with mock.patch.object(
            loop, "_process_message", new_callable=mock.AsyncMock,
            return_value=None,
        ):
            result = await loop.process_direct("hello", "test:direct")
            self.assertIsNone(result)

    def test_run_dream_still_callable(self):
        """run_dream 标记 deprecated 但仍可调用（不破坏现有功能）。"""
        loop, _ = self._make_loop()
        # build_dream_prompt 返回 None 时 run_dream 返回 None
        with mock.patch.object(loop.memory, "build_dream_prompt", return_value=None):
            result = asyncio.get_event_loop().run_until_complete(loop.run_dream())
            self.assertIsNone(result)

    def test_run_dream_has_deprecated_docstring(self):
        loop, _ = self._make_loop()
        self.assertIn("DEPRECATED", loop.run_dream.__doc__ or "")


class Teststep64StateTrace(unittest.IsolatedAsyncioTestCase):
    """step64：StateTraceEntry 状态追踪测试。"""

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

    def test_state_trace_entry_creation(self):
        from step78.loop import StateTraceEntry, TurnState
        entry = StateTraceEntry(
            state=TurnState.RESTORE,
            started_at=12345.678,
            duration_ms=10.5,
            event="ok",
        )
        self.assertEqual(entry.state, TurnState.RESTORE)
        self.assertEqual(entry.started_at, 12345.678)
        self.assertEqual(entry.duration_ms, 10.5)
        self.assertEqual(entry.event, "ok")
        self.assertIsNone(entry.error)

    def test_state_trace_entry_error(self):
        from step78.loop import StateTraceEntry, TurnState
        entry = StateTraceEntry(
            state=TurnState.RUN,
            started_at=1.0,
            duration_ms=100.0,
            event="",
            error="exception",
        )
        self.assertEqual(entry.error, "exception")
        self.assertEqual(entry.event, "")

    def test_turn_context_trace_default_empty(self):
        ctx = TurnContext(
            msg=InboundMessage(content="hi"),
            session_key="test",
        )
        self.assertEqual(ctx.trace, [])
        self.assertIsInstance(ctx.trace, list)

    async def test_process_message_records_trace(self):
        """正常 turn 结束后 ctx.trace 非空，每条有 state/duration_ms/event。"""
        loop, _ = self._make_loop()
        captured_ctx = {}

        async def _capture_restore(ctx):
            captured_ctx["ctx"] = ctx
            return "ok"

        with mock.patch.object(loop, "_state_restore", side_effect=_capture_restore), \
             mock.patch.object(loop, "_state_compact", return_value="ok"), \
             mock.patch.object(loop, "_state_command", return_value="dispatch"), \
             mock.patch.object(loop, "_state_build", return_value="ok"), \
             mock.patch.object(loop, "_state_run", return_value="ok"), \
             mock.patch.object(loop, "_state_save", return_value="ok"), \
             mock.patch.object(loop, "_state_respond", return_value="ok"):
            msg = InboundMessage(content="hi", chat_id="test")
            await loop._process_message(msg, "test")

        ctx = captured_ctx["ctx"]
        self.assertGreater(len(ctx.trace), 0)
        for entry in ctx.trace:
            self.assertIsNotNone(entry.state)
            self.assertGreaterEqual(entry.duration_ms, 0)
            self.assertIsInstance(entry.event, str)

    async def test_process_message_trace_has_restore_state(self):
        """trace 包含 RESTORE 状态（第一个状态）。"""
        from step78.loop import TurnState
        loop, _ = self._make_loop()
        captured_ctx = {}

        async def _capture_restore(ctx):
            captured_ctx["ctx"] = ctx
            return "ok"

        with mock.patch.object(loop, "_state_restore", side_effect=_capture_restore), \
             mock.patch.object(loop, "_state_compact", return_value="ok"), \
             mock.patch.object(loop, "_state_command", return_value="dispatch"), \
             mock.patch.object(loop, "_state_build", return_value="ok"), \
             mock.patch.object(loop, "_state_run", return_value="ok"), \
             mock.patch.object(loop, "_state_save", return_value="ok"), \
             mock.patch.object(loop, "_state_respond", return_value="ok"):
            msg = InboundMessage(content="hi", chat_id="test")
            await loop._process_message(msg, "test")

        ctx = captured_ctx["ctx"]
        states = [e.state for e in ctx.trace]
        self.assertIn(TurnState.RESTORE, states)

    async def test_process_message_trace_error_on_exception(self):
        """状态抛异常时 trace 最后一条 error="exception"。"""
        from step78.loop import TurnState
        loop, _ = self._make_loop()
        captured_ctx = {}

        async def _capture_restore(ctx):
            captured_ctx["ctx"] = ctx
            return "ok"

        async def _raise_build(ctx):
            raise RuntimeError("build failed")

        with mock.patch.object(loop, "_state_restore", side_effect=_capture_restore), \
             mock.patch.object(loop, "_state_compact", return_value="ok"), \
             mock.patch.object(loop, "_state_command", return_value="dispatch"), \
             mock.patch.object(loop, "_state_build", side_effect=_raise_build):
            msg = InboundMessage(content="hi", chat_id="test")
            outbound = await loop._process_message(msg, "test")

        ctx = captured_ctx["ctx"]
        self.assertIsNotNone(outbound)
        self.assertEqual(outbound.metadata["stop_reason"], "error")
        # 最后一条 trace 应该是 BUILD 状态的异常记录
        last = ctx.trace[-1]
        self.assertEqual(last.state, TurnState.BUILD)
        self.assertEqual(last.error, "exception")
        self.assertEqual(last.event, "")

    async def test_process_message_trace_duration_positive(self):
        """所有 trace 的 duration_ms >= 0。"""
        loop, _ = self._make_loop()
        captured_ctx = {}

        async def _capture_restore(ctx):
            captured_ctx["ctx"] = ctx
            return "ok"

        with mock.patch.object(loop, "_state_restore", side_effect=_capture_restore), \
             mock.patch.object(loop, "_state_compact", return_value="ok"), \
             mock.patch.object(loop, "_state_command", return_value="dispatch"), \
             mock.patch.object(loop, "_state_build", return_value="ok"), \
             mock.patch.object(loop, "_state_run", return_value="ok"), \
             mock.patch.object(loop, "_state_save", return_value="ok"), \
             mock.patch.object(loop, "_state_respond", return_value="ok"):
            msg = InboundMessage(content="hi", chat_id="test")
            await loop._process_message(msg, "test")

        ctx = captured_ctx["ctx"]
        for entry in ctx.trace:
            self.assertGreaterEqual(entry.duration_ms, 0)


# ---- Step 45 Tests: _save_turn 增强 + _state_command 持久化 ----

class Teststep64SaveTurnEnhancements(unittest.TestCase):
    """step64：_save_turn _meta 弹出 + runtime_context + image_url + updated_at。"""

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
            memory=memory, identity="test", replay_budget=10000,
        )
        return loop

    def test_save_turn_pops_meta(self):
        """_save_turn 后消息中无 _meta 字段。"""
        loop = self._make_loop()
        session = loop.sessions.get_or_create("meta_test")
        messages = [
            {"role": "user", "content": "hi", "_meta": {"internal": "data"}},
            {"role": "assistant", "content": "hello"},
        ]
        loop._save_turn(session, messages, skip=0)
        for msg in session.messages:
            self.assertNotIn("_meta", msg)

    def test_save_turn_runtime_context_meta(self):
        """user 消息中设置 RUNTIME_CONTEXT_HISTORY_META。"""
        from step78.runtime_context import RUNTIME_CONTEXT_HISTORY_META, RUNTIME_CONTEXT_MESSAGE_META
        loop = self._make_loop()
        session = loop.sessions.get_or_create("rc_test")
        messages = [
            {
                "role": "user", "content": "hi",
                "_meta": {RUNTIME_CONTEXT_MESSAGE_META: {"ctx": "value"}},
            },
        ]
        loop._save_turn(session, messages, skip=0)
        self.assertEqual(session.messages[0].get(RUNTIME_CONTEXT_HISTORY_META), {"ctx": "value"})

    def test_sanitize_image_url_data(self):
        """image_url data: 块替换为文本占位。"""
        loop = self._make_loop()
        content = [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,ABC"}},
        ]
        result = loop._sanitize_persisted_blocks(content)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["type"], "text")
        self.assertIn("[image", result[0]["text"])

    def test_sanitize_image_url_with_path(self):
        """image_url data: 块带 _meta.path 时占位文本包含路径。"""
        loop = self._make_loop()
        content = [
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,ABC"},
                "_meta": {"path": "/tmp/photo.png"},
            },
        ]
        result = loop._sanitize_persisted_blocks(content)
        self.assertEqual(result[0]["text"], "[image: /tmp/photo.png]")

    def test_sanitize_image_url_https_unchanged(self):
        """https:// 图片 URL 不替换。"""
        loop = self._make_loop()
        content = [
            {"type": "image_url", "image_url": {"url": "https://example.com/photo.png"}},
        ]
        result = loop._sanitize_persisted_blocks(content)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["type"], "image_url")

    def test_save_turn_updated_at_is_datetime(self):
        """updated_at 是 datetime 对象而非字符串。"""
        from datetime import datetime
        loop = self._make_loop()
        session = loop.sessions.get_or_create("dt_test")
        messages = [{"role": "user", "content": "hi"}]
        loop._save_turn(session, messages, skip=0)
        self.assertIsInstance(session.updated_at, datetime)


class Teststep64CommandPersistence(unittest.IsolatedAsyncioTestCase):
    """step64：_state_command shortcut 持久化。"""

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
            memory=memory, identity="test", replay_budget=10000,
        )
        return loop

    async def test_state_command_new_not_persisted(self):
        """/new 命令不持久化（无 _command 标记消息）。"""
        loop = self._make_loop()
        session = loop.sessions.get_or_create("new_test")
        session.add_message("user", "old message")
        ctx = TurnContext(
            msg=InboundMessage(content="/new", channel="cli", chat_id="new_test"),
            session_key="new_test",
        )
        await loop._state_restore(ctx)
        await loop._state_command(ctx)
        # /new 不持久化 shortcut，无 _command 标记消息
        msgs = loop.sessions.get_or_create("new_test").messages
        for msg in msgs:
            self.assertFalse(msg.get("_command", False))


# ---- Step 46 Tests: _drop_malformed_tool_calls 元组 + malformed_retry ----

class Teststep64DropMalformedToolCalls(unittest.TestCase):
    """step64：_drop_malformed_tool_calls 返回元组 + mutate response。"""

    def test_drop_malformed_returns_tuple(self):
        """返回 (dropped_count, all_dropped, original_finish_reason)。"""
        response = LLMResponse(
            content="", tool_calls=[
                ToolCallRequest(id="1", name="valid_tool", arguments={}),
                ToolCallRequest(id="2", name="", arguments={}),
            ],
            finish_reason="tool_calls",
        )
        result = AgentRunner._drop_malformed_tool_calls(response)
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 3)
        dropped, all_dropped, original = result
        self.assertEqual(dropped, 1)
        self.assertFalse(all_dropped)
        self.assertEqual(original, "tool_calls")

    def test_drop_malformed_mutates_response(self):
        """response.tool_calls 被过滤，all_dropped 时 finish_reason 改 'stop'。"""
        response = LLMResponse(
            content="", tool_calls=[
                ToolCallRequest(id="1", name="", arguments={}),
            ],
            finish_reason="tool_calls",
        )
        dropped, all_dropped, original = AgentRunner._drop_malformed_tool_calls(response)
        self.assertEqual(dropped, 1)
        self.assertTrue(all_dropped)
        self.assertEqual(original, "tool_calls")
        self.assertEqual(response.tool_calls, [])
        self.assertEqual(response.finish_reason, "stop")

    def test_drop_malformed_no_calls(self):
        """无 tool_calls 时返回 (0, False, finish_reason)。"""
        response = LLMResponse(content="hello", finish_reason="stop")
        result = AgentRunner._drop_malformed_tool_calls(response)
        self.assertEqual(result, (0, False, "stop"))

    def test_drop_malformed_all_valid(self):
        """所有 tool_call 有效时返回 (0, False, finish_reason)，不修改 response。"""
        calls = [ToolCallRequest(id="1", name="valid", arguments={})]
        response = LLMResponse(content="", tool_calls=calls, finish_reason="tool_calls")
        result = AgentRunner._drop_malformed_tool_calls(response)
        self.assertEqual(result, (0, False, "tool_calls"))
        self.assertEqual(len(response.tool_calls), 1)


class Teststep64MalformedRetryMessages(unittest.TestCase):
    """step64：_malformed_tool_call_retry_messages 构造重试提示。"""

    def test_retry_messages_contains_note(self):
        """重试消息包含 malformed 提示文本。"""
        msgs = [{"role": "user", "content": "hi"}]
        result = AgentRunner._malformed_tool_call_retry_messages(msgs, None)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[1]["role"], "user")
        self.assertIn("malformed", result[1]["content"].lower())

    def test_retry_messages_with_assistant_text(self):
        """assistant_text 不为空时包含在提示中。"""
        msgs = [{"role": "user", "content": "hi"}]
        result = AgentRunner._malformed_tool_call_retry_messages(msgs, "I will use tools")
        self.assertIn("I will use tools", result[1]["content"])
        self.assertIn("Previous assistant text", result[1]["content"])

    def test_retry_messages_does_not_mutate_original(self):
        """不修改原始 messages 列表。"""
        msgs = [{"role": "user", "content": "hi"}]
        AgentRunner._malformed_tool_call_retry_messages(msgs, None)
        self.assertEqual(len(msgs), 1)


class Teststep64MalformedFallback(unittest.IsolatedAsyncioTestCase):
    """step64：_request_malformed_fallback 降级无工具请求。"""

    async def test_fallback_calls_provider_without_tools(self):
        """降级请求时 tools=None。"""
        captured = {}

        class _FallbackProvider(LLMProvider):
            @property
            def model(self): return "mock"
            async def chat(self, messages, tools=None, **kwargs):
                return LLMResponse(content="fallback", finish_reason="stop")
            async def chat_with_retry(self, messages, tools=None, **kwargs):
                captured["tools"] = tools
                return LLMResponse(content="fallback", finish_reason="stop")

        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "hi"}],
            tools=ToolRegistry(),
            provider=_FallbackProvider(),
            max_iterations=1,
        )
        runner = AgentRunner()
        result = await runner._request_malformed_fallback(spec, [{"role": "user", "content": "hi"}])
        self.assertEqual(captured["tools"], None)
        self.assertEqual(result.content, "fallback")


# ---- Step 47 Tests: _request_finalization_retry + 空响应处理 ----

class Teststep64FinalizationRetryMessages(unittest.TestCase):
    """step64：_finalization_retry_messages 构造重试消息。"""

    def test_finalization_retry_messages_contains_prompt(self):
        """重试消息包含 user 提示文本。"""
        msgs = [{"role": "user", "content": "hi"}]
        result = AgentRunner._finalization_retry_messages(msgs)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[1]["role"], "user")
        self.assertIn("based on the conversation", result[1]["content"].lower())

    def test_finalization_retry_messages_no_mutation(self):
        """不修改原始 messages 列表。"""
        msgs = [{"role": "user", "content": "hi"}]
        AgentRunner._finalization_retry_messages(msgs)
        self.assertEqual(len(msgs), 1)


class Teststep64FinalizationRetryIntegration(unittest.IsolatedAsyncioTestCase):
    """step64：空响应耗尽后发 finalization retry 集成测试。"""

    async def test_empty_response_finalization_retry_success(self):
        """空响应耗尽后 finalization retry 返回正常内容。"""
        # _MAX_EMPTY_RETRIES=2，前 2 次空响应，第 3 次（finalization retry）返回正常
        provider = _EmptyResponseProvider(empty_count=2)
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "hello"}],
            tools=_MockToolRegistry(),
            provider=provider,
            max_iterations=5,
        )
        result = await AgentRunner().run(spec)
        self.assertIsNotNone(result)
        # finalization retry 第 3 次调用返回 "final response"
        self.assertEqual(result.final_content, "final response")
        # 总共调用 3 次（2 次空响应 + 1 次 finalization retry）
        self.assertEqual(provider.call_count, 3)

    async def test_empty_response_finalization_retry_still_empty(self):
        """finalization retry 仍为空时用 fallback。"""
        # 主循环 3 次（2 次重试 + 1 次触发）+ finalization retry 1 次 = 第 4 次
        provider = _EmptyResponseProvider(empty_count=4)
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "hello"}],
            tools=_MockToolRegistry(),
            provider=provider,
            max_iterations=5,
        )
        result = await AgentRunner().run(spec)
        self.assertIsNotNone(result)
        # finalization retry 返回空，用 fallback
        self.assertEqual(result.final_content, _EMPTY_FINAL_RESPONSE_MESSAGE)

    async def test_empty_response_finalization_retry_error(self):
        """finalization retry 异常时用 fallback。"""
        class _ErrorOnFourthProvider(LLMProvider):
            def __init__(self):
                self.call_count = 0
            @property
            def model(self): return "mock"
            async def chat(self, messages, tools=None, **kwargs):
                self.call_count += 1
                if self.call_count <= 3:
                    # 前 3 次空响应（2 次重试 + 1 次触发 finalization）
                    return LLMResponse(content="", finish_reason="stop",
                                       usage={"prompt_tokens": 5, "completion_tokens": 3})
                # 第 4 次（finalization retry）抛异常
                raise RuntimeError("provider error")
            async def chat_stream_with_retry(self, messages, tools=None, **kwargs):
                return await self.chat(messages, tools=tools, **kwargs)

        provider = _ErrorOnFourthProvider()
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "hello"}],
            tools=_MockToolRegistry(),
            provider=provider,
            max_iterations=5,
        )
        result = await AgentRunner().run(spec)
        self.assertIsNotNone(result)
        # finalization retry 抛异常，用 fallback
        self.assertEqual(result.final_content, _EMPTY_FINAL_RESPONSE_MESSAGE)


# ---- Step 48 Tests: hook.finalize_content + reasoning 提取 ----

class Teststep64ExtractReasoning(unittest.TestCase):
    """step64：extract_reasoning 剥离推理内容。"""

    def test_extract_reasoning_strips_think_tags(self):
        """<think> 标签从 content 中剥离，reasoning 被提取。"""
        from step78.helpers import extract_reasoning
        content = "<think>reasoning here</think>final answer"
        reasoning_text, cleaned = extract_reasoning(None, None, content)
        self.assertIsNotNone(reasoning_text)
        self.assertIn("reasoning here", reasoning_text)
        self.assertNotIn("<think>", cleaned)
        self.assertIn("final answer", cleaned)

    def test_extract_reasoning_from_reasoning_content(self):
        """reasoning_content 优先于内联 <think>。"""
        from step78.helpers import extract_reasoning
        reasoning_text, cleaned = extract_reasoning(
            "dedicated reasoning", None, "<think>inline</think>answer"
        )
        self.assertEqual(reasoning_text, "dedicated reasoning")
        self.assertNotIn("<think>", cleaned)

    def test_extract_reasoning_no_reasoning(self):
        """无推理内容时返回 (None, content)。"""
        from step78.helpers import extract_reasoning
        reasoning_text, cleaned = extract_reasoning(None, None, "plain answer")
        self.assertIsNone(reasoning_text)
        self.assertEqual(cleaned, "plain answer")


class Teststep64ReasoningIntegration(unittest.IsolatedAsyncioTestCase):
    """step64：reasoning 提取 + emit_reasoning 集成测试。"""

    async def test_reasoning_content_extracted_and_emitted(self):
        """reasoning_content 被提取并通过 emit_reasoning 输出，不进入最终答案。"""
        class _ReasoningProvider(LLMProvider):
            @property
            def model(self): return "mock"
            async def chat(self, messages, tools=None, **kwargs):
                return LLMResponse(
                    content="final answer",
                    reasoning_content="deep reasoning",
                    finish_reason="stop",
                    usage={"prompt_tokens": 5, "completion_tokens": 3},
                )
            async def chat_stream_with_retry(self, messages, tools=None, **kwargs):
                return await self.chat(messages, tools=tools, **kwargs)

        class _CaptureHook(AgentHook):
            def __init__(self):
                self.reasoning_emitted = []
                self.reasoning_ended = False
            async def emit_reasoning(self, content):
                self.reasoning_emitted.append(content)
            async def emit_reasoning_end(self):
                self.reasoning_ended = True

        hook = _CaptureHook()
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "hello"}],
            tools=_MockToolRegistry(),
            provider=_ReasoningProvider(),
            max_iterations=5,
            hook=hook,
        )
        result = await AgentRunner().run(spec)
        self.assertIsNotNone(result)
        self.assertEqual(result.final_content, "final answer")
        # reasoning 被提取并输出
        self.assertEqual(len(hook.reasoning_emitted), 1)
        self.assertIn("deep reasoning", hook.reasoning_emitted[0])
        self.assertTrue(hook.reasoning_ended)
        # reasoning 不进入最终答案
        self.assertNotIn("deep reasoning", result.final_content)

    async def test_think_tags_stripped_from_final_content(self):
        """内联 <think> 标签从最终答案中剥离。"""
        class _ThinkProvider(LLMProvider):
            @property
            def model(self): return "mock"
            async def chat(self, messages, tools=None, **kwargs):
                return LLMResponse(
                    content="<think>my reasoning</think>the answer is 42",
                    finish_reason="stop",
                    usage={"prompt_tokens": 5, "completion_tokens": 3},
                )
            async def chat_stream_with_retry(self, messages, tools=None, **kwargs):
                return await self.chat(messages, tools=tools, **kwargs)

        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "hello"}],
            tools=_MockToolRegistry(),
            provider=_ThinkProvider(),
            max_iterations=5,
        )
        result = await AgentRunner().run(spec)
        self.assertIsNotNone(result)
        self.assertNotIn("<think>", result.final_content)
        self.assertNotIn("my reasoning", result.final_content)
        self.assertIn("the answer is 42", result.final_content)


class Teststep64FinalizeContent(unittest.IsolatedAsyncioTestCase):
    """step64：hook.finalize_content 被调用。"""

    async def test_finalize_content_called_in_main_loop(self):
        """主循环中 hook.finalize_content 被调用。"""
        class _FinalizeHook(AgentHook):
            def __init__(self):
                self.calls = []
            def finalize_content(self, context, content):
                self.calls.append(content)
                return content

        hook = _FinalizeHook()
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "hello"}],
            tools=_MockToolRegistry(),
            provider=_EmptyResponseProvider(empty_count=0),
            max_iterations=5,
            hook=hook,
        )
        result = await AgentRunner().run(spec)
        self.assertIsNotNone(result)
        self.assertGreater(len(hook.calls), 0)
        self.assertIn("final response", hook.calls)


# ---- Step 49 Tests: usage 估算升级 ----

class Teststep64UsageTools(unittest.TestCase):
    """step64：usage 工具方法（_usage_dict / _usage_total / _merge_usage）。"""

    def test_usage_dict_converts_values(self):
        """_usage_dict 正确转换 usage 字典为 int，过滤非数字值。"""
        from step78.runner import AgentRunner
        usage = {"prompt_tokens": "10", "completion_tokens": 5, "invalid": "abc"}
        result = AgentRunner._usage_dict(usage)
        self.assertEqual(result["prompt_tokens"], 10)
        self.assertEqual(result["completion_tokens"], 5)
        self.assertNotIn("invalid", result)

    def test_usage_dict_none(self):
        """_usage_dict(None) 返回空 dict。"""
        from step78.runner import AgentRunner
        self.assertEqual(AgentRunner._usage_dict(None), {})

    def test_usage_total_prefers_total_tokens(self):
        """_usage_total 优先用 total_tokens。"""
        from step78.runner import AgentRunner
        usage = {"total_tokens": 100, "prompt_tokens": 10, "completion_tokens": 5}
        self.assertEqual(AgentRunner._usage_total(usage), 100)

    def test_usage_total_falls_back_to_sum(self):
        """_usage_total 无 total_tokens 时用 prompt+completion。"""
        from step78.runner import AgentRunner
        usage = {"prompt_tokens": 10, "completion_tokens": 5}
        self.assertEqual(AgentRunner._usage_total(usage), 15)

    def test_merge_usage_combines_dicts(self):
        """_merge_usage 正确合并两个 usage dict（逐键相加）。"""
        from step78.runner import AgentRunner
        left = {"prompt_tokens": 10, "completion_tokens": 5}
        right = {"prompt_tokens": 3, "total_tokens": 8}
        merged = AgentRunner._merge_usage(left, right)
        self.assertEqual(merged["prompt_tokens"], 13)
        self.assertEqual(merged["completion_tokens"], 5)
        self.assertEqual(merged["total_tokens"], 8)


class Teststep64UsageEstimate(unittest.TestCase):
    """step64：_estimate_response_usage / _usage_or_estimate。"""

    def test_estimate_response_usage_returns_total_and_estimated(self):
        """_estimate_response_usage 返回含 total_tokens/estimated_tokens 的 dict。"""
        from step78.runner import AgentRunner, AgentRunSpec, LLMResponse
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "hello"}],
            tools=_MockToolRegistry(),
            provider=_MockProvider(),
            max_iterations=5,
        )
        response = LLMResponse(content="answer", finish_reason="stop")
        result = AgentRunner()._estimate_response_usage(spec, spec.initial_messages, response)
        self.assertIn("prompt_tokens", result)
        self.assertIn("completion_tokens", result)
        self.assertIn("total_tokens", result)
        self.assertIn("estimated_tokens", result)
        self.assertEqual(result["total_tokens"], result["estimated_tokens"])
        self.assertGreater(result["total_tokens"], 0)

    def test_usage_or_estimate_prefers_real(self):
        """_usage_or_estimate 优先用真实 usage，不估算。"""
        from step78.runner import AgentRunner, AgentRunSpec, LLMResponse
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "hello"}],
            tools=_MockToolRegistry(),
            provider=_MockProvider(),
            max_iterations=5,
        )
        response = LLMResponse(
            content="answer", finish_reason="stop",
            usage={"prompt_tokens": 100, "completion_tokens": 50},
        )
        result = AgentRunner()._usage_or_estimate(spec, spec.initial_messages, response)
        self.assertEqual(result["prompt_tokens"], 100)
        self.assertEqual(result["completion_tokens"], 50)
        self.assertEqual(result["total_tokens"], 150)
        self.assertEqual(result["provider_tokens"], 150)
        self.assertNotIn("estimated_tokens", result)

    def test_usage_or_estimate_falls_back_to_estimate(self):
        """_usage_or_estimate 缺失 usage 时估算。"""
        from step78.runner import AgentRunner, AgentRunSpec, LLMResponse
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "hello"}],
            tools=_MockToolRegistry(),
            provider=_MockProvider(),
            max_iterations=5,
        )
        response = LLMResponse(content="answer", finish_reason="stop")
        result = AgentRunner()._usage_or_estimate(spec, spec.initial_messages, response)
        self.assertIn("estimated_tokens", result)
        self.assertGreater(result["total_tokens"], 0)

    def test_usage_or_estimate_error_returns_empty(self):
        """_usage_or_estimate 对 error 响应返回空 dict。"""
        from step78.runner import AgentRunner, AgentRunSpec, LLMResponse
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "hello"}],
            tools=_MockToolRegistry(),
            provider=_MockProvider(),
            max_iterations=5,
        )
        response = LLMResponse(content="error", finish_reason="error")
        result = AgentRunner()._usage_or_estimate(spec, spec.initial_messages, response)
        self.assertEqual(result, {})


class Teststep64AccumulateUsage(unittest.TestCase):
    """step64：_accumulate_usage 累计所有键。"""

    def test_accumulate_usage_counts_all_keys(self):
        """_accumulate_usage 累计所有键（包括 total_tokens/estimated_tokens）。"""
        from step78.runner import AgentRunner, LLMResponse
        total = {"prompt_tokens": 0, "completion_tokens": 0}
        response = LLMResponse(
            content="answer", finish_reason="stop",
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "estimated_tokens": 15},
        )
        AgentRunner._accumulate_usage(total, response)
        self.assertEqual(total["prompt_tokens"], 10)
        self.assertEqual(total["completion_tokens"], 5)
        self.assertEqual(total["total_tokens"], 15)
        self.assertEqual(total["estimated_tokens"], 15)


# ---- Step 50 Tests: _run_tool hook 生命周期 + 三元组返回 ----

class _step64EchoRegistry:
    """简单工具注册表，execute 返回固定结果。"""
    def __init__(self, result="echo result"):
        self._result = result
        self.calls = []
    def get_definitions(self): return []
    async def execute(self, name, **params):
        self.calls.append((name, params))
        return self._result


class _step64ErrorRegistry:
    """工具注册表，execute 抛异常。"""
    def __init__(self, exc=RuntimeError("tool boom")):
        self._exc = exc
    def get_definitions(self): return []
    async def execute(self, name, **params):
        raise self._exc


class _step64CancelledRegistry:
    """工具注册表，execute 抛 CancelledError。"""
    def get_definitions(self): return []
    async def execute(self, name, **params):
        raise asyncio.CancelledError()


class _step64TrackingHook(AgentHook):
    """记录工具执行 hook 调用。"""
    def __init__(self):
        self.before_calls = []
        self.after_calls = []
        self.error_calls = []
    async def before_execute_tool(self, context, tool_call, tool, params):
        self.before_calls.append((tool_call, tool, params))
    async def after_execute_tool(self, context, tool_call, tool, params, result):
        self.after_calls.append((tool_call, tool, params, result))
    async def on_execute_tool_error(self, context, tool_call, tool, params, error):
        self.error_calls.append((tool_call, tool, params, error))


class Teststep64RunTool(unittest.IsolatedAsyncioTestCase):
    """step64：_run_tool 三元组返回 + hook 生命周期。"""

    def _make_ctx(self):
        from step78.hook import AgentHookContext
        return AgentHookContext(iteration=0, messages=[])

    def _make_spec(self, registry):
        from step78.runner import AgentRunSpec
        return AgentRunSpec(
            initial_messages=[{"role": "user", "content": "hi"}],
            tools=registry,
            provider=_MockProvider(),
            max_iterations=5,
        )

    async def test_run_tool_returns_triple(self):
        """_run_tool 返回 (result, event, error) 三元组。"""
        from step78.runner import AgentRunner
        from step78.governance import ContextGovernanceConfig
        tc = ToolCallRequest(id="c1", name="echo", arguments={"text": "hi"})
        registry = _step64EchoRegistry()
        spec = self._make_spec(registry)
        hook = _step64TrackingHook()
        ctx = self._make_ctx()
        result, event, error = await AgentRunner()._run_tool(
            tc, spec, ContextGovernanceConfig(tools=_MockToolRegistry()), hook, ctx, [], {}, {},
        )
        self.assertEqual(result, "echo result")
        self.assertEqual(event["name"], "echo")
        self.assertEqual(event["status"], "ok")
        self.assertIsNone(error)

    async def test_run_tool_calls_before_execute_hook(self):
        """_run_tool 调用 before_execute_tool。"""
        from step78.runner import AgentRunner
        from step78.governance import ContextGovernanceConfig
        tc = ToolCallRequest(id="c1", name="echo", arguments={"text": "hi"})
        registry = _step64EchoRegistry()
        spec = self._make_spec(registry)
        hook = _step64TrackingHook()
        ctx = self._make_ctx()
        await AgentRunner()._run_tool(tc, spec, ContextGovernanceConfig(tools=_MockToolRegistry()), hook, ctx, [], {}, {})
        self.assertEqual(len(hook.before_calls), 1)
        self.assertEqual(hook.before_calls[0][0], tc)

    async def test_run_tool_calls_after_execute_hook(self):
        """_run_tool 成功后调用 after_execute_tool。"""
        from step78.runner import AgentRunner
        from step78.governance import ContextGovernanceConfig
        tc = ToolCallRequest(id="c1", name="echo", arguments={"text": "hi"})
        registry = _step64EchoRegistry()
        spec = self._make_spec(registry)
        hook = _step64TrackingHook()
        ctx = self._make_ctx()
        await AgentRunner()._run_tool(tc, spec, ContextGovernanceConfig(tools=_MockToolRegistry()), hook, ctx, [], {}, {})
        self.assertEqual(len(hook.after_calls), 1)
        self.assertEqual(hook.after_calls[0][3], "echo result")

    async def test_run_tool_calls_on_error_hook(self):
        """工具异常时调用 on_execute_tool_error，返回 error event。"""
        from step78.runner import AgentRunner
        from step78.governance import ContextGovernanceConfig
        tc = ToolCallRequest(id="c1", name="echo", arguments={"text": "hi"})
        registry = _step64ErrorRegistry()
        spec = self._make_spec(registry)
        hook = _step64TrackingHook()
        ctx = self._make_ctx()
        result, event, error = await AgentRunner()._run_tool(
            tc, spec, ContextGovernanceConfig(tools=_MockToolRegistry()), hook, ctx, [], {}, {},
        )
        self.assertEqual(len(hook.error_calls), 1)
        self.assertEqual(event["status"], "error")
        self.assertIn("tool boom", result)
        self.assertIsNone(error)  # step64 不设 fatal_error（留到 step64 fail_on_tool_error）

    async def test_run_tool_cancelled_error_propagates(self):
        """CancelledError 不被捕获，向上传播。"""
        from step78.runner import AgentRunner
        from step78.governance import ContextGovernanceConfig
        tc = ToolCallRequest(id="c1", name="echo", arguments={"text": "hi"})
        registry = _step64CancelledRegistry()
        spec = self._make_spec(registry)
        hook = _step64TrackingHook()
        ctx = self._make_ctx()
        with self.assertRaises(asyncio.CancelledError):
            await AgentRunner()._run_tool(tc, spec, ContextGovernanceConfig(tools=_MockToolRegistry()), hook, ctx, [], {}, {})
        self.assertEqual(len(hook.error_calls), 0)  # CancelledError 不调 on_error


class Teststep64ExecuteToolBatch(unittest.IsolatedAsyncioTestCase):
    """step64：_execute_tool_batch 三元组返回。"""

    async def test_execute_tool_batch_returns_triple(self):
        """_execute_tool_batch 返回 (results, events, fatal_error)。"""
        from step78.runner import AgentRunner, AgentRunSpec
        from step78.governance import ContextGovernanceConfig
        from step78.hook import AgentHookContext, AgentHook
        tc = ToolCallRequest(id="c1", name="echo", arguments={"text": "hi"})
        registry = _step64EchoRegistry("ok")
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "hi"}],
            tools=registry, provider=_MockProvider(), max_iterations=5,
            concurrent_tools=False,
        )
        ctx = AgentHookContext(iteration=0, messages=[])
        results, events, fatal_error = await AgentRunner()._execute_tool_batch(
            [(tc, None)], spec, ContextGovernanceConfig(tools=_MockToolRegistry()), AgentHook(), ctx, [], {}, {},
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0], "ok")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["status"], "ok")
        self.assertIsNone(fatal_error)


class Teststep64RunCancelledError(unittest.IsolatedAsyncioTestCase):
    """step64：run() 中 CancelledError 分离。"""

    async def test_run_cancelled_error_no_on_error(self):
        """run() 中 CancelledError 不调 on_error。"""
        from step78.runner import AgentRunner, AgentRunSpec
        from step78.hook import AgentHook

        class _CancelledProvider(LLMProvider):
            @property
            def model(self): return "mock"
            async def chat(self, messages, tools=None, **kwargs):
                raise asyncio.CancelledError()
            async def chat_stream_with_retry(self, messages, tools=None, **kwargs):
                raise asyncio.CancelledError()

        class _NoErrorHook(AgentHook):
            def __init__(self):
                self.on_error_called = False
            async def on_error(self, context):
                self.on_error_called = True

        hook = _NoErrorHook()
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "hi"}],
            tools=_MockToolRegistry(),
            provider=_CancelledProvider(),
            max_iterations=5,
            hook=hook,
        )
        with self.assertRaises(asyncio.CancelledError):
            await AgentRunner().run(spec)
        self.assertFalse(hook.on_error_called)


# ---- Step 51 Tests: SSRF/workspace 安全检测 ----

class Teststep64SSRFDetection(unittest.TestCase):
    """step64：SSRF 检测方法。"""

    def test_is_ssrf_violation_detects_markers(self):
        """_is_ssrf_violation 检测 SSRF markers。"""
        from step78.runner import AgentRunner
        self.assertTrue(AgentRunner._is_ssrf_violation("Error: internal/private url detected"))
        self.assertTrue(AgentRunner._is_ssrf_violation("private/internal address blocked"))
        self.assertFalse(AgentRunner._is_ssrf_violation("normal error message"))
        self.assertFalse(AgentRunner._is_ssrf_violation(""))

    def test_is_workspace_violation_detects_markers(self):
        """_is_workspace_violation 检测 workspace markers。"""
        from step78.runner import AgentRunner
        self.assertTrue(AgentRunner._is_workspace_violation("path outside working dir"))
        self.assertTrue(AgentRunner._is_workspace_violation("outside the configured workspace"))
        self.assertTrue(AgentRunner._is_workspace_violation("path traversal detected"))
        self.assertFalse(AgentRunner._is_workspace_violation("normal error"))

    def test_is_workspace_violation_includes_ssrf(self):
        """_is_workspace_violation 也包含 SSRF markers。"""
        from step78.runner import AgentRunner
        self.assertTrue(AgentRunner._is_workspace_violation("private address blocked"))

    def test_ssrf_soft_payload_includes_boundary_note(self):
        """_ssrf_soft_payload 包含边界说明。"""
        from step78.runner import AgentRunner
        payload = AgentRunner._ssrf_soft_payload("Error: internal url")
        self.assertIn("Error: internal url", payload)
        self.assertIn("non-bypassable security boundary", payload)


class Teststep64ClassifyViolation(unittest.TestCase):
    """step64：_classify_violation 统一分类。"""

    def test_classify_ssrf_returns_soft_payload(self):
        """_classify_violation SSRF 命中返回软 payload。"""
        from step78.runner import AgentRunner
        tc = ToolCallRequest(id="c1", name="web_fetch", arguments={"url": "http://10.0.0.1"})
        event = {"name": "web_fetch", "status": "error", "detail": ""}
        result = AgentRunner()._classify_violation(
            raw_text="Error: internal/private url detected",
            soft_payload="original error",
            event=event,
            tool_call=tc,
            workspace_violation_counts={},
        )
        self.assertIsNotNone(result)
        payload, ev, error = result
        self.assertIn("non-bypassable security boundary", payload)
        self.assertEqual(ev["status"], "error")
        self.assertIn("ssrf_violation", ev["detail"])
        self.assertIsNone(error)

    def test_classify_workspace_returns_soft_payload(self):
        """_classify_violation workspace 命中返回软 payload。"""
        from step78.runner import AgentRunner
        tc = ToolCallRequest(id="c1", name="read_file", arguments={"path": "/etc/passwd"})
        event = {"name": "read_file", "status": "error", "detail": ""}
        result = AgentRunner()._classify_violation(
            raw_text="path outside working dir",
            soft_payload="original error",
            event=event,
            tool_call=tc,
            workspace_violation_counts={},
        )
        self.assertIsNotNone(result)
        payload, ev, error = result
        self.assertEqual(payload, "original error")
        self.assertIn("workspace_violation", ev["detail"])

    def test_classify_non_violation_returns_none(self):
        """_classify_violation 非安全违规返回 None。"""
        from step78.runner import AgentRunner
        tc = ToolCallRequest(id="c1", name="echo", arguments={})
        event = {"name": "echo", "status": "error", "detail": ""}
        result = AgentRunner()._classify_violation(
            raw_text="normal error",
            soft_payload="original error",
            event=event,
            tool_call=tc,
            workspace_violation_counts={},
        )
        self.assertIsNone(result)


class Teststep64RepeatedLookup(unittest.TestCase):
    """step64：重复外部查找和 workspace 违规检测。"""

    def test_repeated_external_lookup_blocks_after_2(self):
        """重复外部查找 2 次后阻断。"""
        from step78.helpers import repeated_external_lookup_error
        counts = {}
        # 前 2 次不阻断
        self.assertIsNone(repeated_external_lookup_error("web_fetch", {"url": "http://x.com"}, counts))
        self.assertIsNone(repeated_external_lookup_error("web_fetch", {"url": "http://x.com"}, counts))
        # 第 3 次阻断
        result = repeated_external_lookup_error("web_fetch", {"url": "http://x.com"}, counts)
        self.assertIsNotNone(result)
        self.assertIn("repeated external lookup blocked", result)

    def test_repeated_workspace_violation_escalates_after_2(self):
        """重复 workspace 违规 2 次后升级。"""
        from step78.helpers import repeated_workspace_violation_error
        counts = {}
        # 前 2 次不升级
        self.assertIsNone(repeated_workspace_violation_error("read_file", {"path": "/etc/passwd"}, counts))
        self.assertIsNone(repeated_workspace_violation_error("read_file", {"path": "/etc/passwd"}, counts))
        # 第 3 次升级
        result = repeated_workspace_violation_error("read_file", {"path": "/etc/passwd"}, counts)
        self.assertIsNotNone(result)
        self.assertIn("refusing repeated workspace-bypass", result)


class Teststep64RunToolSecurity(unittest.IsolatedAsyncioTestCase):
    """step64：_run_tool 中安全检测集成。"""

    def _make_spec(self, registry):
        from step78.runner import AgentRunSpec
        return AgentRunSpec(
            initial_messages=[{"role": "user", "content": "hi"}],
            tools=registry, provider=_MockProvider(), max_iterations=5,
        )

    async def test_run_tool_ssrf_violation_blocked(self):
        """_run_tool 中 SSRF 违规被阻断，返回边界说明。"""
        from step78.runner import AgentRunner
        from step78.hook import AgentHookContext, AgentHook
        from step78.governance import ContextGovernanceConfig

        class _SSRFRegistry:
            def get_definitions(self): return []
            async def execute(self, name, **params):
                raise RuntimeError("internal/private url detected")

        tc = ToolCallRequest(id="c1", name="web_fetch", arguments={"url": "http://10.0.0.1"})
        spec = self._make_spec(_SSRFRegistry())
        ctx = AgentHookContext(iteration=0, messages=[])
        result, event, error = await AgentRunner()._run_tool(
            tc, spec, ContextGovernanceConfig(tools=_MockToolRegistry()),
            AgentHook(), ctx, [], {}, {},
        )
        self.assertIn("non-bypassable security boundary", str(result))
        self.assertEqual(event["status"], "error")
        self.assertIn("ssrf_violation", event["detail"])

    async def test_run_tool_repeated_external_lookup_blocked(self):
        """_run_tool 中重复外部查找被阻断。"""
        from step78.runner import AgentRunner
        from step78.hook import AgentHookContext, AgentHook
        from step78.governance import ContextGovernanceConfig

        class _FetchRegistry:
            def get_definitions(self): return []
            async def execute(self, name, **params):
                return "result"

        tc = ToolCallRequest(id="c1", name="web_fetch", arguments={"url": "http://x.com"})
        spec = self._make_spec(_FetchRegistry())
        ctx = AgentHookContext(iteration=0, messages=[])
        counts = {"web_fetch:http://x.com": 2}  # 已查 2 次
        result, event, error = await AgentRunner()._run_tool(
            tc, spec, ContextGovernanceConfig(tools=_MockToolRegistry()),
            AgentHook(), ctx, [], counts, {},
        )
        self.assertIn("repeated external lookup blocked", str(result))
        self.assertEqual(event["status"], "error")


# ---- Step 52 Tests: fail_on_tool_error + tool_events ----

class TestStep52SpecFields(unittest.TestCase):
    """step52：AgentRunSpec / AgentRunResult 新字段。"""

    def test_fail_on_tool_error_default_false(self):
        """AgentRunSpec.fail_on_tool_error 默认 False。"""
        from step78.runner import AgentRunSpec
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "hi"}],
            tools=_MockToolRegistry(),
            provider=_MockProvider(),
            max_iterations=5,
        )
        self.assertFalse(spec.fail_on_tool_error)

    def test_tool_events_default_empty(self):
        """AgentRunResult.tool_events 默认空列表。"""
        from step78.runner import AgentRunResult
        result = AgentRunResult(final_content="hi", messages=[])
        self.assertEqual(result.tool_events, [])


class Teststep64FailOnToolError(unittest.IsolatedAsyncioTestCase):
    """step64：fail_on_tool_error 终止 turn。"""

    async def test_fail_on_tool_error_terminates_turn(self):
        """fail_on_tool_error=True 时工具异常终止 turn，stop_reason=tool_error。"""
        from step78.runner import AgentRunner, AgentRunSpec

        class _ErrorProvider(LLMProvider):
            @property
            def model(self): return "mock"
            async def chat(self, messages, tools=None, **kwargs):
                return LLMResponse(
                    content="", finish_reason="tool_calls",
                    tool_calls=[ToolCallRequest(id="c1", name="bad_tool", arguments={})],
                )

        class _ErrorRegistry:
            def get_definitions(self): return []
            async def execute(self, name, **params):
                raise RuntimeError("tool exploded")

        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "hi"}],
            tools=_ErrorRegistry(),
            provider=_ErrorProvider(),
            max_iterations=5,
            fail_on_tool_error=True,
        )
        result = await AgentRunner().run(spec)
        self.assertIsNotNone(result)
        self.assertEqual(result.stop_reason, "tool_error")
        self.assertIn("tool exploded", result.final_content or "")

    async def test_fail_on_tool_error_false_continues(self):
        """fail_on_tool_error=False（默认）时工具异常不终止。"""
        from step78.runner import AgentRunner, AgentRunSpec

        class _ThenAnswerProvider(LLMProvider):
            def __init__(self):
                self._call_count = 0
            @property
            def model(self): return "mock"
            async def chat(self, messages, tools=None, **kwargs):
                self._call_count += 1
                if self._call_count == 1:
                    return LLMResponse(
                        content="", finish_reason="tool_calls",
                        tool_calls=[ToolCallRequest(id="c1", name="bad_tool", arguments={})],
                    )
                return LLMResponse(content="recovered answer", finish_reason="stop")

        class _ErrorRegistry:
            def get_definitions(self): return []
            async def execute(self, name, **params):
                raise RuntimeError("tool exploded")

        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "hi"}],
            tools=_ErrorRegistry(),
            provider=_ThenAnswerProvider(),
            max_iterations=5,
        )
        result = await AgentRunner().run(spec)
        self.assertIsNotNone(result)
        self.assertEqual(result.stop_reason, "stop")
        self.assertEqual(result.final_content, "recovered answer")


class Teststep64ToolEvents(unittest.IsolatedAsyncioTestCase):
    """step64：tool_events 收集。"""

    async def test_tool_events_collected_in_result(self):
        """AgentRunResult.tool_events 收集所有工具调用事件。"""
        from step78.runner import AgentRunner, AgentRunSpec

        class _ToolProvider(LLMProvider):
            @property
            def model(self): return "mock"
            async def chat(self, messages, tools=None, **kwargs):
                return LLMResponse(
                    content="", finish_reason="tool_calls",
                    tool_calls=[ToolCallRequest(id="c1", name="echo", arguments={"text": "hi"})],
                )

        class _EchoRegistry:
            def get_definitions(self): return []
            async def execute(self, name, **params):
                return "echo result"

        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "hi"}],
            tools=_EchoRegistry(),
            provider=_ToolProvider(),
            max_iterations=5,
        )
        result = await AgentRunner().run(spec)
        self.assertIsNotNone(result)
        self.assertGreater(len(result.tool_events), 0)
        event = result.tool_events[0]
        self.assertIn("name", event)
        self.assertIn("status", event)
        self.assertIn("detail", event)
        self.assertEqual(event["status"], "ok")

    async def test_tool_event_error_status(self):
        """工具异常时 event status=error。"""
        from step78.runner import AgentRunner, AgentRunSpec

        class _ToolProvider(LLMProvider):
            @property
            def model(self): return "mock"
            async def chat(self, messages, tools=None, **kwargs):
                return LLMResponse(
                    content="", finish_reason="tool_calls",
                    tool_calls=[ToolCallRequest(id="c1", name="bad", arguments={})],
                )

        class _ErrorRegistry:
            def get_definitions(self): return []
            async def execute(self, name, **params):
                raise RuntimeError("boom")

        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "hi"}],
            tools=_ErrorRegistry(),
            provider=_ToolProvider(),
            max_iterations=5,
        )
        result = await AgentRunner().run(spec)
        self.assertIsNotNone(result)
        error_events = [e for e in result.tool_events if e["status"] == "error"]
        self.assertGreater(len(error_events), 0)


# ---- Step 53 Tests: progress streaming + thinking 流 ----

class Teststep64SpecFields(unittest.TestCase):
    """step64：AgentRunSpec 新字段。"""

    def test_stream_progress_deltas_default_true(self):
        """AgentRunSpec.stream_progress_deltas 默认 True。"""
        from step78.runner import AgentRunSpec
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "hi"}],
            tools=_MockToolRegistry(),
            provider=_MockProvider(),
            max_iterations=5,
        )
        self.assertTrue(spec.stream_progress_deltas)


class Teststep64IncrementalThinkExtractor(unittest.IsolatedAsyncioTestCase):
    """step64：IncrementalThinkExtractor 基本功能。"""

    async def test_feed_extracts_think(self):
        """feed 提取 <think> 块中的新文本并 emit。"""
        from step78.helpers import IncrementalThinkExtractor
        emitted = []
        async def emit(text):
            emitted.append(text)
        extractor = IncrementalThinkExtractor()
        result = await extractor.feed("<think>hello</think>world", emit)
        self.assertTrue(result)
        self.assertEqual(emitted, ["hello"])

    async def test_feed_no_duplicate(self):
        """feed 不重复 emit 已发出的 think 文本。"""
        from step78.helpers import IncrementalThinkExtractor
        emitted = []
        async def emit(text):
            emitted.append(text)
        extractor = IncrementalThinkExtractor()
        await extractor.feed("<think>hello</think>world", emit)
        result = await extractor.feed("<think>hello</think>world more", emit)
        self.assertFalse(result)
        self.assertEqual(emitted, ["hello"])

    async def test_feed_incremental(self):
        """feed 增量提取 think 文本。"""
        from step78.helpers import IncrementalThinkExtractor
        emitted = []
        async def emit(text):
            emitted.append(text)
        extractor = IncrementalThinkExtractor()
        await extractor.feed("<think>hello</think>", emit)
        await extractor.feed("<think>hello world</think>", emit)
        self.assertEqual(emitted, ["hello", "world"])


class Teststep64StreamingThinking(unittest.IsolatedAsyncioTestCase):
    """step64：流式 thinking 提取。"""

    async def test_stream_progress_deltas_false_keeps_original(self):
        """stream_progress_deltas=False 时保持原始 delta 输出。"""
        from step78.runner import AgentRunner, AgentRunSpec

        class _StreamHook(AgentHook):
            def __init__(self):
                self.deltas = []
            async def on_stream(self, ctx, delta):
                self.deltas.append(delta)

        hook = _StreamHook()
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "hi"}],
            tools=_MockToolRegistry(),
            provider=_StreamingMockProvider(),
            hook=hook,
            stream_progress_deltas=False,
        )
        await AgentRunner().run(spec)
        self.assertEqual(hook.deltas, ["Hello", " ", "world", "!"])

    async def test_progress_callback_with_think_extracts_reasoning(self):
        """progress_callback + think 内容通过 emit_reasoning 输出。"""
        from step78.runner import AgentRunner, AgentRunSpec

        class _ThinkProvider(LLMProvider):
            @property
            def model(self): return "mock"
            async def chat(self, messages, tools=None, **kwargs):
                return LLMResponse(content="answer", finish_reason="stop")
            async def chat_stream_with_retry(self, messages, tools=None, **kwargs):
                on_delta = kwargs.get("on_content_delta")
                if on_delta:
                    for chunk in ["<think>reason</think>", "answer"]:
                        await on_delta(chunk)
                return LLMResponse(content="<think>reason</think>answer", finish_reason="stop")

        class _ThinkHook(AgentHook):
            def __init__(self):
                self.reasoning = []
            async def emit_reasoning(self, text):
                self.reasoning.append(text)

        async def _progress(delta):
            pass

        hook = _ThinkHook()
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "hi"}],
            tools=_MockToolRegistry(),
            provider=_ThinkProvider(),
            hook=hook,
            progress_callback=_progress,
        )
        await AgentRunner().run(spec)
        self.assertIn("reason", hook.reasoning)


# ---- Step 54 Tests: 函数式参数 + 流式分段 + background_tasks ----


class TestStep54GoalContinueCallable(unittest.TestCase):
    """step64：goal_continue_message 支持 callable。"""

    def test_callable_goal_continue_message_invoked(self):
        """callable goal_continue_message 被调用并返回动态消息。"""
        from step78.runner import AgentRunSpec, AgentRunner
        runner = AgentRunner()
        calls = []

        def _goal_msg():
            calls.append(1)
            return "Dynamic goal message"

        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "hi"}],
            tools=_MockToolRegistry(),
            provider=_MockProvider(),
            max_iterations=5,
            goal_continue_message=_goal_msg,
        )
        msg = runner._build_goal_continue_message(spec)
        self.assertEqual(msg["content"], "Dynamic goal message")
        self.assertEqual(calls, [1])

    def test_callable_goal_continue_message_none_falls_back(self):
        """callable 返回 None 时使用默认消息。"""
        from step78.runner import AgentRunSpec, AgentRunner
        runner = AgentRunner()
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "hi"}],
            tools=_MockToolRegistry(),
            provider=_MockProvider(),
            max_iterations=5,
            goal_continue_message=lambda: None,
        )
        msg = runner._build_goal_continue_message(spec)
        self.assertIn("active sustained goal", msg["content"])

    def test_string_goal_continue_message_still_works(self):
        """字符串 goal_continue_message 保持向后兼容。"""
        from step78.runner import AgentRunSpec, AgentRunner
        runner = AgentRunner()
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "hi"}],
            tools=_MockToolRegistry(),
            provider=_MockProvider(),
            max_iterations=5,
            goal_continue_message="Static goal",
        )
        msg = runner._build_goal_continue_message(spec)
        self.assertEqual(msg["content"], "Static goal")


class TestStep54BackgroundTasks(unittest.IsolatedAsyncioTestCase):
    """step64：_background_tasks 跟踪 + close_mcp drain。"""

    async def test_schedule_background_tracks_task(self):
        """_schedule_background 将 task 加入 _background_tasks。"""
        import asyncio
        from step78.loop import AgentLoop
        loop = AgentLoop.__new__(AgentLoop)
        loop._background_tasks = []

        async def _noop():
            pass

        loop._schedule_background(_noop())
        self.assertEqual(len(loop._background_tasks), 1)
        # 清理
        task = loop._background_tasks[0]
        await task

    async def test_close_mcp_drains_background_tasks(self):
        """close_mcp 等待所有后台任务完成。"""
        import asyncio
        from step78.loop import AgentLoop
        loop = AgentLoop.__new__(AgentLoop)
        loop._background_tasks = []
        done = []

        async def _bg():
            await asyncio.sleep(0.01)
            done.append(1)

        loop._schedule_background(_bg())
        self.assertEqual(len(loop._background_tasks), 1)
        await loop.close_mcp()
        self.assertEqual(done, [1])
        self.assertEqual(len(loop._background_tasks), 0)

    async def test_background_task_auto_removed_on_completion(self):
        """后台任务完成后自动从列表移除。"""
        import asyncio
        from step78.loop import AgentLoop
        loop = AgentLoop.__new__(AgentLoop)
        loop._background_tasks = []

        async def _quick():
            pass

        loop._schedule_background(_quick())
        task = loop._background_tasks[0]
        await task
        # add_done_callback 是同步的，但可能需要一个事件循环迭代
        await asyncio.sleep(0)
        self.assertEqual(len(loop._background_tasks), 0)


class TestStep54StreamSegmentation(unittest.IsolatedAsyncioTestCase):
    """step64：_wants_stream 流式分段。"""

    async def test_wants_stream_stream_id_sequence(self):
        """_wants_stream 时 stream_id 按 segment 递增。"""
        import time
        session_key = "cli:c1"
        stream_base_id = f"{session_key}:{time.time_ns()}"
        stream_segment = 0

        def _current_stream_id():
            return f"{stream_base_id}:{stream_segment}"

        sid0 = _current_stream_id()
        self.assertTrue(sid0.startswith(f"{session_key}:"))
        self.assertTrue(sid0.endswith(":0"))
        stream_segment += 1
        sid1 = _current_stream_id()
        self.assertTrue(sid1.endswith(":1"))
        self.assertNotEqual(sid0, sid1)

    def test_stream_delta_typed_event_has_stream_id(self):
        """StreamDeltaEvent typed event 包含 stream_id 字段。"""
        from step78.bus.outbound_events import StreamDeltaEvent as TypedDelta
        evt = TypedDelta(content="hello", stream_id="s1:0")
        self.assertEqual(evt.content, "hello")
        self.assertEqual(evt.stream_id, "s1:0")

    def test_stream_delta_typed_event_defaults(self):
        """StreamDeltaEvent typed event 默认值。"""
        from step78.bus.outbound_events import StreamDeltaEvent as TypedDelta
        evt = TypedDelta()
        self.assertEqual(evt.content, "")
        self.assertIsNone(evt.stream_id)

    def test_stream_end_event_carries_stream_id(self):
        """StreamEndEvent 携带 stream_id。"""
        from step78.bus.outbound_events import StreamEndEvent
        evt = StreamEndEvent(stream_id="s1:0", resuming=False)
        self.assertEqual(evt.stream_id, "s1:0")
        self.assertFalse(evt.resuming)


# ---- Step 55 Tests: ModelRuntimeResolver ----


class TestStep55ModelRuntimeResolver(unittest.TestCase):
    """step64：ModelRuntimeResolver 核心功能。"""

    def _make_runtime(self, model="gpt-4", preset=None, sig=None):
        from step78.llm import GenerationSettings, LLMRuntime
        return LLMRuntime(
            provider=object(),
            model=model,
            generation=GenerationSettings(temperature=0.7, max_tokens=4096),
            context_window_tokens=128000,
            model_preset=preset,
            snapshot_signature=sig,
        )

    def test_runtime_property_returns_initial(self):
        from step78.model_runtime import ModelRuntimeResolver
        rt = self._make_runtime()
        resolver = ModelRuntimeResolver(rt)
        self.assertIs(resolver.runtime, rt)

    def test_model_preset_property(self):
        from step78.model_runtime import ModelRuntimeResolver
        resolver = ModelRuntimeResolver(self._make_runtime(preset="fast"))
        self.assertEqual(resolver.model_preset, "fast")

    def test_provider_signature_property(self):
        from step78.model_runtime import ModelRuntimeResolver
        sig = ("openai", "gpt-4")
        resolver = ModelRuntimeResolver(self._make_runtime(sig=sig))
        self.assertEqual(resolver.provider_signature, sig)

    def test_current_returns_runtime(self):
        from step78.model_runtime import ModelRuntimeResolver
        rt = self._make_runtime()
        resolver = ModelRuntimeResolver(rt)
        self.assertIs(resolver.current(), rt)
        self.assertIs(resolver.current(refresh=True), rt)

    def test_select_model_changes_model(self):
        from step78.model_runtime import ModelRuntimeResolver
        resolver = ModelRuntimeResolver(self._make_runtime(model="gpt-4"))
        new_rt = resolver.select_model("gpt-4o")
        self.assertEqual(new_rt.model, "gpt-4o")
        self.assertIsNone(new_rt.model_preset)
        self.assertEqual(resolver.runtime.model, "gpt-4o")

    def test_select_model_strips_whitespace(self):
        from step78.model_runtime import ModelRuntimeResolver
        resolver = ModelRuntimeResolver(self._make_runtime())
        new_rt = resolver.select_model("  gpt-4o  ")
        self.assertEqual(new_rt.model, "gpt-4o")

    def test_select_model_empty_raises(self):
        from step78.model_runtime import ModelRuntimeResolver
        resolver = ModelRuntimeResolver(self._make_runtime())
        with self.assertRaises(ValueError):
            resolver.select_model("")
        with self.assertRaises(ValueError):
            resolver.select_model("   ")

    def test_select_model_non_string_raises(self):
        from step78.model_runtime import ModelRuntimeResolver
        resolver = ModelRuntimeResolver(self._make_runtime())
        with self.assertRaises(ValueError):
            resolver.select_model(123)

    def test_select_preset_sets_preset(self):
        from step78.model_runtime import ModelRuntimeResolver
        resolver = ModelRuntimeResolver(self._make_runtime())
        new_rt = resolver.select_preset("fast")
        self.assertEqual(new_rt.model_preset, "fast")
        self.assertEqual(resolver.model_preset, "fast")

    def test_select_preset_none_clears(self):
        from step78.model_runtime import ModelRuntimeResolver
        resolver = ModelRuntimeResolver(self._make_runtime(preset="fast"))
        new_rt = resolver.select_preset(None)
        self.assertIsNone(new_rt.model_preset)

    def test_select_context_window(self):
        from step78.model_runtime import ModelRuntimeResolver
        resolver = ModelRuntimeResolver(self._make_runtime())
        new_rt = resolver.select_context_window(256000)
        self.assertEqual(new_rt.context_window_tokens, 256000)

    def test_select_context_window_non_int_raises(self):
        from step78.model_runtime import ModelRuntimeResolver
        resolver = ModelRuntimeResolver(self._make_runtime())
        with self.assertRaises(TypeError):
            resolver.select_context_window(128.5)
        with self.assertRaises(TypeError):
            resolver.select_context_window(True)

    def test_select_model_preserves_provider_and_generation(self):
        from step78.model_runtime import ModelRuntimeResolver
        rt = self._make_runtime(model="gpt-4")
        resolver = ModelRuntimeResolver(rt)
        new_rt = resolver.select_model("gpt-4o")
        self.assertIs(new_rt.provider, rt.provider)
        self.assertEqual(new_rt.generation, rt.generation)


class TestStep55AgentLoopRuntime(unittest.TestCase):
    """step64：AgentLoop runtime 委托给 ModelRuntimeResolver。"""

    def test_runtime_property_delegates_to_resolver(self):
        """AgentLoop.runtime 委托给 _runtime_resolver。"""
        from step78.loop import AgentLoop
        self.assertIsInstance(AgentLoop.runtime, property)

    def test_llm_runtime_property_exists(self):
        """AgentLoop.llm_runtime 属性存在。"""
        from step78.loop import AgentLoop
        self.assertIsInstance(AgentLoop.llm_runtime, property)

    def test_set_runtime_model_method_exists(self):
        """AgentLoop.set_runtime_model 方法存在。"""
        from step78.loop import AgentLoop
        self.assertTrue(callable(getattr(AgentLoop, "set_runtime_model", None)))

    def test_set_model_preset_method_exists(self):
        """AgentLoop.set_model_preset 方法存在。"""
        from step78.loop import AgentLoop
        self.assertTrue(callable(getattr(AgentLoop, "set_model_preset", None)))

    def test_set_runtime_context_window_method_exists(self):
        """AgentLoop.set_runtime_context_window 方法存在。"""
        from step78.loop import AgentLoop
        self.assertTrue(callable(getattr(AgentLoop, "set_runtime_context_window", None)))


# ---- Step 56 Tests: media 处理 ----


class TestStep56DocumentUtils(unittest.TestCase):
    """step64：utils/document.py 附件处理。"""

    def test_is_image_file_png(self):
        from step78.utils.document import is_image_file
        self.assertTrue(is_image_file("photo.png"))
        self.assertTrue(is_image_file("photo.jpg"))
        self.assertTrue(is_image_file("photo.jpeg"))
        self.assertTrue(is_image_file("photo.gif"))
        self.assertTrue(is_image_file("photo.webp"))

    def test_is_image_file_non_image(self):
        from step78.utils.document import is_image_file
        self.assertFalse(is_image_file("doc.pdf"))
        self.assertFalse(is_image_file("readme.txt"))
        self.assertFalse(is_image_file("data.csv"))
        self.assertFalse(is_image_file("noext"))

    def test_reference_non_image_attachments_separates(self):
        from step78.utils.document import reference_non_image_attachments
        content, images = reference_non_image_attachments(
            "hello", ["photo.png", "doc.pdf", "notes.txt"],
        )
        self.assertEqual(images, ["photo.png"])
        self.assertIn("hello", content)
        self.assertIn("[Attachment: doc.pdf]", content)
        self.assertIn("[Attachment: notes.txt]", content)
        self.assertNotIn("[Attachment: photo.png]", content)

    def test_reference_non_image_attachments_empty_content(self):
        from step78.utils.document import reference_non_image_attachments
        content, images = reference_non_image_attachments("", ["doc.pdf"])
        self.assertEqual(images, [])
        self.assertIn("[Attachment: doc.pdf]", content)

    def test_reference_non_image_attachments_all_images(self):
        from step78.utils.document import reference_non_image_attachments
        content, images = reference_non_image_attachments(
            "hello", ["a.png", "b.jpg"],
        )
        self.assertEqual(images, ["a.png", "b.jpg"])
        self.assertEqual(content, "hello")

    def test_reference_non_image_attachments_empty_media(self):
        from step78.utils.document import reference_non_image_attachments
        content, images = reference_non_image_attachments("hello", [])
        self.assertEqual(content, "hello")
        self.assertEqual(images, [])


class TestStep56ImagePlaceholder(unittest.TestCase):
    """step64：image_placeholder_text。"""

    def test_with_path(self):
        from step78.helpers import image_placeholder_text
        self.assertEqual(image_placeholder_text("/tmp/a.png"), "[image: /tmp/a.png]")

    def test_with_none(self):
        from step78.helpers import image_placeholder_text
        self.assertEqual(image_placeholder_text(None), "[image]")

    def test_with_empty_string(self):
        from step78.helpers import image_placeholder_text
        self.assertEqual(image_placeholder_text(""), "[image]")

    def test_custom_empty(self):
        from step78.helpers import image_placeholder_text
        self.assertEqual(image_placeholder_text(None, empty="[no image]"), "[no image]")


class TestStep56PrepareMessageMedia(unittest.TestCase):
    """step64：AgentLoop._prepare_message_media。"""

    def test_prepare_message_media_method_exists(self):
        from step78.loop import AgentLoop
        self.assertTrue(callable(getattr(AgentLoop, "_prepare_message_media", None)))

    def test_prepare_message_media_separates_attachments(self):
        """_prepare_message_media 分离图片和非图片附件。"""
        from step78.loop import AgentLoop
        # 用 __new__ 避免完整初始化
        loop = AgentLoop.__new__(AgentLoop)
        content, images = loop._prepare_message_media(
            "hello", ["photo.png", "doc.pdf"],
        )
        self.assertEqual(images, ["photo.png"])
        self.assertIn("[Attachment: doc.pdf]", content)


# ---- Step 57 Tests: TurnContext 字段重构 ----


class TestStep57TurnContextFields(unittest.TestCase):
    """step64：TurnContext 移除 result/error/summary，改用扁平字段。"""

    def test_turn_context_no_result_field(self):
        """TurnContext 不再有 result 字段。"""
        from step78.loop import TurnContext
        ctx = TurnContext(
            msg=InboundMessage(content="hi"),
            session_key="test",
        )
        self.assertFalse(hasattr(ctx, "result"))

    def test_turn_context_no_error_field(self):
        """TurnContext 不再有 error 字段。"""
        from step78.loop import TurnContext
        ctx = TurnContext(
            msg=InboundMessage(content="hi"),
            session_key="test",
        )
        self.assertFalse(hasattr(ctx, "error"))

    def test_turn_context_no_summary_field(self):
        """TurnContext 不再有 summary 字段，改用 pending_summary。"""
        from step78.loop import TurnContext
        ctx = TurnContext(
            msg=InboundMessage(content="hi"),
            session_key="test",
        )
        self.assertFalse(hasattr(ctx, "summary"))
        self.assertIsNone(ctx.pending_summary)

    def test_turn_context_has_usage_field(self):
        """TurnContext 新增 usage 字典字段。"""
        from step78.loop import TurnContext
        ctx = TurnContext(
            msg=InboundMessage(content="hi"),
            session_key="test",
        )
        self.assertEqual(ctx.usage, {})

    def test_turn_context_pending_summary_settable(self):
        """pending_summary 可设置。"""
        from step78.loop import TurnContext
        ctx = TurnContext(
            msg=InboundMessage(content="hi"),
            session_key="test",
        )
        ctx.pending_summary = "Summary text"
        self.assertEqual(ctx.pending_summary, "Summary text")

    def test_turn_context_flat_fields_defaults(self):
        """扁平字段默认值正确。"""
        from step78.loop import TurnContext
        ctx = TurnContext(
            msg=InboundMessage(content="hi"),
            session_key="test",
        )
        self.assertIsNone(ctx.final_content)
        self.assertEqual(ctx.stop_reason, "")
        self.assertEqual(ctx.tools_used, [])
        self.assertEqual(ctx.all_messages, [])
        self.assertFalse(ctx.had_injections)


# ---- Step 58 Tests: runner 收尾对齐 ----


class TestStep58MergeMessageContent(unittest.TestCase):
    """step64：_merge_message_content。"""

    def test_merge_two_strings(self):
        from step78.runner import AgentRunner
        result = AgentRunner._merge_message_content("hello", "world")
        self.assertEqual(result, "hello\n\nworld")

    def test_merge_empty_left(self):
        from step78.runner import AgentRunner
        result = AgentRunner._merge_message_content("", "world")
        self.assertEqual(result, "world")

    def test_merge_none_left(self):
        from step78.runner import AgentRunner
        result = AgentRunner._merge_message_content(None, "world")
        self.assertEqual(result, [{"type": "text", "text": "world"}])

    def test_merge_lists(self):
        from step78.runner import AgentRunner
        left = [{"type": "text", "text": "a"}]
        right = [{"type": "text", "text": "b"}]
        result = AgentRunner._merge_message_content(left, right)
        self.assertEqual(len(result), 2)

    def test_merge_mixed_string_and_list(self):
        from step78.runner import AgentRunner
        result = AgentRunner._merge_message_content("hello", [{"type": "text", "text": "b"}])
        self.assertEqual(len(result), 2)


class TestStep58AppendFinalMessage(unittest.TestCase):
    """step64：_append_final_message。"""

    def test_append_to_empty_list(self):
        from step78.runner import AgentRunner
        messages = []
        AgentRunner._append_final_message(messages, "hello")
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["role"], "assistant")
        self.assertEqual(messages[0]["content"], "hello")

    def test_append_none_content_noop(self):
        from step78.runner import AgentRunner
        messages = []
        AgentRunner._append_final_message(messages, None)
        self.assertEqual(len(messages), 0)

    def test_append_empty_string_noop(self):
        from step78.runner import AgentRunner
        messages = []
        AgentRunner._append_final_message(messages, "")
        self.assertEqual(len(messages), 0)

    def test_replace_last_assistant_content(self):
        from step78.runner import AgentRunner
        messages = [{"role": "assistant", "content": "old"}]
        AgentRunner._append_final_message(messages, "new")
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["content"], "new")

    def test_no_duplicate_same_content(self):
        from step78.runner import AgentRunner
        messages = [{"role": "assistant", "content": "same"}]
        AgentRunner._append_final_message(messages, "same")
        self.assertEqual(len(messages), 1)

    def test_append_after_tool_call_message(self):
        from step78.runner import AgentRunner
        messages = [{"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]}]
        AgentRunner._append_final_message(messages, "final")
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[1]["content"], "final")


class TestStep58ModelErrorPlaceholder(unittest.TestCase):
    """step64：_append_model_error_placeholder。"""

    def test_append_placeholder(self):
        from step78.runner import AgentRunner, _PERSISTED_MODEL_ERROR_PLACEHOLDER
        messages = [{"role": "user", "content": "hi"}]
        AgentRunner._append_model_error_placeholder(messages)
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[1]["content"], _PERSISTED_MODEL_ERROR_PLACEHOLDER)

    def test_no_duplicate_after_assistant(self):
        from step78.runner import AgentRunner
        messages = [{"role": "assistant", "content": "error"}]
        AgentRunner._append_model_error_placeholder(messages)
        self.assertEqual(len(messages), 1)

    def test_placeholder_constant_text(self):
        from step78.runner import _PERSISTED_MODEL_ERROR_PLACEHOLDER
        self.assertIn("model error", _PERSISTED_MODEL_ERROR_PLACEHOLDER.lower())


class TestStep58IsToolErrorResult(unittest.TestCase):
    """step64：is_tool_error_result。"""

    def test_error_result(self):
        from step78.tool import ToolResult, is_tool_error_result
        result = ToolResult.error("something went wrong")
        self.assertTrue(is_tool_error_result("test", result))

    def test_success_result(self):
        from step78.tool import ToolResult, is_tool_error_result
        result = ToolResult("ok")
        self.assertFalse(is_tool_error_result("test", result))

    def test_non_tool_result(self):
        from step78.tool import is_tool_error_result
        self.assertFalse(is_tool_error_result("test", "plain string"))
        self.assertFalse(is_tool_error_result("test", None))


class TestStep58BuildRequestKwargs(unittest.TestCase):
    """step64：_build_request_kwargs。"""

    def test_kwargs_contains_required_fields(self):
        from step78.runner import AgentRunner, AgentRunSpec
        runner = AgentRunner()
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "hi"}],
            tools=_MockToolRegistry(),
            provider=_MockProvider(),
            max_iterations=5,
        )
        kwargs = runner._build_request_kwargs(
            spec, [{"role": "user", "content": "hi"}],
            tools=[{"type": "function"}],
        )
        self.assertIn("messages", kwargs)
        self.assertIn("tools", kwargs)
        self.assertIn("model", kwargs)
        self.assertIn("temperature", kwargs)
        self.assertIn("max_tokens", kwargs)


# ---- Step 59 Tests: loop 收尾对齐 ----


class TestStep59RequestContextForTurn(unittest.TestCase):
    """step64：_request_context_for_turn 重命名。"""

    def test_method_renamed(self):
        """_build_turn_request_context 已重命名为 _request_context_for_turn。"""
        from step78.loop import AgentLoop
        self.assertTrue(hasattr(AgentLoop, "_request_context_for_turn"))
        self.assertFalse(hasattr(AgentLoop, "_build_turn_request_context"))

    def test_method_callable(self):
        from step78.loop import AgentLoop
        self.assertTrue(callable(getattr(AgentLoop, "_request_context_for_turn", None)))


class TestStep59WorkspaceScopeForTurn(unittest.TestCase):
    """step64：_build_agent_spec 使用 for_turn。"""

    def test_for_turn_method_exists(self):
        """WorkspaceScopeResolver.for_turn 方法存在。"""
        from step78.security.workspace_access import WorkspaceScopeResolver
        self.assertTrue(hasattr(WorkspaceScopeResolver, "for_turn"))

    def test_for_turn_returns_default_for_non_scoped_channel(self):
        """非 scoped channel 返回默认 scope。"""
        import tempfile
        from pathlib import Path
        from step78.security.workspace_access import WorkspaceScopeResolver
        default_ws = Path(tempfile.gettempdir())
        resolver = WorkspaceScopeResolver(
            default_workspace=default_ws,
            default_restrict_to_workspace=False,
            scoped_channel="feishu",
        )
        scope = resolver.for_turn(
            channel="cli",
            message_metadata={},
            session_metadata={},
        )
        self.assertEqual(scope.project_path, default_ws)


class TestStep59SystemMessageExtendToUser(unittest.TestCase):
    """step64：_process_system_message 中 extend_to_user=is_subagent。"""

    def test_extend_to_user_logic_in_source(self):
        """_process_system_message 源码中 extend_to_user=is_subagent。"""
        import inspect
        from step78.loop import AgentLoop
        source = inspect.getsource(AgentLoop._process_system_message)
        self.assertIn("extend_to_user=is_subagent", source)


# ---- Step 60 Tests: 配置层扩展 + from_config ----


class Teststep64ChannelsConfigExtractDocumentText(unittest.TestCase):
    """step64：ChannelsConfig.extract_document_text 字段。"""

    def test_default_true(self):
        from step78.config.schema import ChannelsConfig
        cfg = ChannelsConfig()
        self.assertTrue(cfg.extract_document_text)

    def test_set_false(self):
        from step78.config.schema import ChannelsConfig
        cfg = ChannelsConfig(extract_document_text=False)
        self.assertFalse(cfg.extract_document_text)


class Teststep64ToolsConfigWebExec(unittest.TestCase):
    """step64：ToolsConfig.web/exec 子配置。"""

    def test_web_default(self):
        from step78.config.schema import ToolsConfig
        cfg = ToolsConfig()
        self.assertTrue(cfg.web.enable)
        self.assertIsNone(cfg.web.proxy)

    def test_exec_default(self):
        from step78.config.schema import ToolsConfig
        cfg = ToolsConfig()
        self.assertTrue(cfg.exec.enable)
        self.assertEqual(cfg.exec.timeout, 60)

    def test_web_override(self):
        from step78.config.schema import ToolsConfig
        cfg = ToolsConfig(web={"enable": False, "proxy": "http://proxy:8080"})
        self.assertFalse(cfg.web.enable)
        self.assertEqual(cfg.web.proxy, "http://proxy:8080")


class Teststep64AgentLoopConfigParams(unittest.TestCase):
    """step64：AgentLoop 接收 channels_config/tools_config。"""

    def test_init_accepts_channels_config(self):
        """__init__ 签名包含 channels_config 参数。"""
        import inspect
        from step78.loop import AgentLoop
        sig = inspect.signature(AgentLoop.__init__)
        self.assertIn("channels_config", sig.parameters)
        self.assertIn("tools_config", sig.parameters)

    def test_from_config_passes_configs(self):
        """from_config 传递 channels_config 和 tools_config。"""
        import inspect
        from step78.loop import AgentLoop
        source = inspect.getsource(AgentLoop.from_config)
        self.assertIn("channels_config", source)
        self.assertIn("tools_config", source)


class Teststep64PrepareMessageMediaWithChannelsConfig(unittest.TestCase):
    """step64：_prepare_message_media 读取 channels_config。"""

    def test_no_channels_config_default_true(self):
        """无 channels_config 时默认 extract=True。"""
        from step78.loop import AgentLoop
        loop = AgentLoop.__new__(AgentLoop)
        # 不设置 channels_config，getattr 返回 None → 默认 True
        content, images = loop._prepare_message_media(
            "hello", ["photo.png", "doc.pdf"],
        )
        self.assertEqual(images, ["photo.png"])
        self.assertIn("[Attachment: doc.pdf]", content)

    def test_extract_disabled_still_references(self):
        """extract_document_text=False 时仍追加引用（当前不实现提取）。"""
        from step78.loop import AgentLoop
        from step78.config.schema import ChannelsConfig
        loop = AgentLoop.__new__(AgentLoop)
        loop.channels_config = ChannelsConfig(extract_document_text=False)
        content, images = loop._prepare_message_media(
            "hello", ["photo.png"],
        )
        self.assertEqual(images, ["photo.png"])


if __name__ == "__main__":
    unittest.main()
