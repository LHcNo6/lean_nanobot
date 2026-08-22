"""Step 26 事件层测试（H4：typed outbound events + RuntimeEventBus）。

全部使用构造数据 / mock provider，禁止真实 API Key。覆盖：
- ``bus/outbound_events.py``：6 个事件类型（字段/默认值/frozen）；
  ``outbound_message_for_event``（content 推导 / metadata 路由）；
- ``bus/runtime_events.py``：RuntimeEventBus（订阅/通道/类型过滤/
  publish_nowait / 异步 handler 等待 / 异常隔离）、RuntimeEventPublisher
  （record/clear、三种派发）、ensure_runtime_event_publisher；
- ``bus/progress.py``：build_bus_progress_callback —— 发布 ProgressEvent；
- provider ``on_retry_wait`` 心跳（chat / chat_stream 重试）；
- loop 集成：turn 完成后 session_turn_started / run_status_changed
  running→idle / turn_completed，重试时 outbound 出现 RetryWaitEvent，
  工具进度出现 ProgressEvent，最终消息挂 StreamedResponseEvent；
- manager 路由：StreamEnd —— send_delta(stream_end)、Progress —— send 门控；
- cli 通道：运行时事件不结束 turn，最终消息才 set _turn_done。
"""

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError

import pytest

from step96.bus import MessageBus
from step96.bus.events import InboundMessage, OutboundMessage, StreamDeltaEvent
from step96.bus.outbound_events import (
    GoalStatusEvent,
    OutboundEvent,
    ProgressEvent,
    RetryWaitEvent,
    StreamEndEvent,
    StreamedResponseEvent,
    TurnEndEvent,
    outbound_message_for_event,
)
from step96.bus.progress import build_bus_progress_callback
from step96.bus.runtime_events import (
    RuntimeEventBus,
    RuntimeEventPublisher,
    RuntimeEventContext,
    SessionTurnStarted,
    TurnCompleted,
    TurnRunStatusChanged,
    ensure_runtime_event_publisher,
)
from step96.channels.cli import CliChannel
from step96.context import ContextBuilder
from step96.llm import LLMResponse, RetryConfig, ToolCallRequest
from step96.loop import AgentLoop
from step96.manager import ChannelManager
from step96.memory import MemoryStore
from step96.provider import LLMProvider
from step96.session import SessionManager
from step96.tool import ToolRegistry
from step96.tools.echo import EchoTool


def _ctx() -> RuntimeEventContext:
    """构造最小的 RuntimeEventContext。"""

    return RuntimeEventContext(channel="cli", chat_id="c1", session_key="k1", metadata={})


# ---------------------------------------------------------------------------
# bus/outbound_events.py
# ---------------------------------------------------------------------------


class TestOutboundEvents:
    @pytest.mark.parametrize(
        "cls",
        [
            ProgressEvent,
            RetryWaitEvent,
            StreamEndEvent,
            StreamedResponseEvent,
            TurnEndEvent,
            GoalStatusEvent,
        ],
    )
    def test_all_subclass_marker(self, cls):
        event = cls(status="active") if cls is GoalStatusEvent else cls()
        assert isinstance(event, OutboundEvent)

    def test_progress_defaults(self):
        event = ProgressEvent()
        assert event.content == ""
        assert event.tool_hint is False
        assert event.reasoning is False
        assert event.reasoning_delta is False
        assert event.reasoning_end is False
        assert event.stream_id is None
        assert event.tool_events is None
        assert event.file_edit_events is None

    def test_retry_wait_defaults(self):
        assert RetryWaitEvent().content == ""

    def test_stream_end_fields(self):
        event = StreamEndEvent(stream_id="s1", resuming=True)
        assert event.stream_id == "s1"
        assert event.resuming is True

    def test_turn_end_fields(self):
        event = TurnEndEvent(latency_ms=123, goal_state={"active": False})
        assert event.latency_ms == 123
        assert event.goal_state == {"active": False}

    def test_goal_status_fields(self):
        event = GoalStatusEvent(status="active", started_at=1.5)
        assert event.status == "active"
        assert event.started_at == 1.5

    def test_frozen_events(self):
        with pytest.raises(FrozenInstanceError):
            ProgressEvent().content = "mutated"  # type: ignore[misc]

    def test_outbound_message_for_event_derives_content(self):
        msg = outbound_message_for_event(
            channel="cli",
            chat_id="c1",
            event=RetryWaitEvent(content="retry in 2s"),
        )
        assert msg.channel == "cli"
        assert msg.chat_id == "c1"
        assert msg.content == "retry in 2s"
        assert isinstance(msg.event, RetryWaitEvent)

    def test_outbound_message_for_event_empty_for_lifecycle(self):
        msg = outbound_message_for_event(channel="cli", chat_id="c1", event=TurnEndEvent())
        assert msg.content == ""

    def test_outbound_message_for_event_content_override(self):
        msg = outbound_message_for_event(
            channel="cli",
            chat_id="c1",
            event=ProgressEvent(content="running tool"),
            content="overridden",
        )
        assert msg.content == "overridden"

    def test_outbound_message_for_event_metadata_copied(self):
        msg = outbound_message_for_event(
            channel="cli",
            chat_id="c1",
            event=ProgressEvent(content="x"),
            metadata={"message_id": "m1"},
        )
        assert msg.metadata == {"message_id": "m1"}


# ---------------------------------------------------------------------------
# bus/runtime_events.py
# ---------------------------------------------------------------------------


class TestRuntimeEventBus:
    @pytest.mark.asyncio
    async def test_subscribe_and_publish(self):
        bus = RuntimeEventBus()
        seen: list[str] = []
        bus.subscribe(lambda e: seen.append(type(e).__name__))
        await bus.publish(SessionTurnStarted(context=_ctx()))
        await bus.publish(TurnCompleted(context=_ctx(), latency_ms=5))
        assert seen == ["SessionTurnStarted", "TurnCompleted"]

    @pytest.mark.asyncio
    async def test_event_type_filter(self):
        bus = RuntimeEventBus()
        seen: list[str] = []
        bus.subscribe(lambda e: seen.append("started"), event_type=SessionTurnStarted)
        await bus.publish(TurnRunStatusChanged(context=_ctx(), status="running"))
        await bus.publish(SessionTurnStarted(context=_ctx()))
        assert seen == ["started"]

    @pytest.mark.asyncio
    async def test_async_handler_awaited_in_order(self):
        bus = RuntimeEventBus()
        order: list[int] = []

        async def async_handler(event) -> None:
            await asyncio.sleep(0.01)
            order.append(1)

        bus.subscribe(lambda e: order.append(0))
        bus.subscribe(async_handler)
        await bus.publish(TurnCompleted(context=_ctx()))
        assert order == [0, 1]

    @pytest.mark.asyncio
    async def test_handler_exception_isolated(self):
        bus = RuntimeEventBus()
        events: list[str] = []

        def bad_handler(event) -> None:
            raise RuntimeError("boom")

        bus.subscribe(bad_handler)
        bus.subscribe(lambda e: events.append("good"))
        await bus.publish(SessionTurnStarted(context=_ctx()))
        assert events == ["good"]

    @pytest.mark.asyncio
    async def test_unsubscribe(self):
        bus = RuntimeEventBus()
        seen: list[str] = []
        unsub = bus.subscribe(lambda e: seen.append("x"))
        unsub()
        await bus.publish(TurnCompleted(context=_ctx()))
        await bus.publish(TurnCompleted(context=_ctx()))
        assert seen == []

    @pytest.mark.asyncio
    async def test_publish_nowait_schedules_task(self):
        bus = RuntimeEventBus()
        seen: list[str] = []
        bus.subscribe(lambda event: seen.append("x"))
        bus.publish_nowait(SessionTurnStarted(context=_ctx()))
        for _ in range(100):
            if seen:
                break
            await asyncio.sleep(0.01)
        assert seen == ["x"]


class TestRuntimeEventPublisher:
    @pytest.mark.asyncio
    async def test_session_turn_started(self):
        bus = RuntimeEventBus()
        seen: list[SessionTurnStarted] = []
        bus.subscribe(seen.append, event_type=SessionTurnStarted)
        publisher = RuntimeEventPublisher(bus)
        msg = InboundMessage(content="hi", channel="cli", chat_id="c1")
        await publisher.session_turn_started(msg, "k1")
        assert len(seen) == 1
        assert seen[0].context.channel == "cli"
        assert seen[0].context.chat_id == "c1"
        assert seen[0].context.session_key == "k1"

    @pytest.mark.asyncio
    async def test_run_status_changed(self):
        bus = RuntimeEventBus()
        seen: list[TurnRunStatusChanged] = []
        bus.subscribe(seen.append, event_type=TurnRunStatusChanged)
        publisher = RuntimeEventPublisher(bus)
        msg = InboundMessage(content="hi", chat_id="c1")
        await publisher.run_status_changed(msg, "s1", "running", started_at=3.0)
        assert seen[0].status == "running"
        assert seen[0].started_at == 3.0
        assert seen[0].context.session_key == "s1"

    @pytest.mark.asyncio
    async def test_turn_completed_pops_latency_and_runtime(self):
        bus = RuntimeEventBus()
        seen: list[TurnCompleted] = []
        bus.subscribe(seen.append, event_type=TurnCompleted)
        publisher = RuntimeEventPublisher(bus)
        runtime = object()
        publisher.record_turn_runtime("k1", runtime)
        publisher.record_turn_latency("k1", 42)
        await publisher.turn_completed(channel="cli", chat_id="c1", session_key="k1", metadata={})
        assert seen[0].latency_ms == 42
        assert seen[0].runtime is runtime
        # 弹出丢弃后再派发，latency 为 None
        await publisher.turn_completed(channel="cli", chat_id="c1", session_key="k1", metadata={})
        assert seen[1].latency_ms is None

    @pytest.mark.asyncio
    async def test_clear_turn_removes_staging(self):
        publisher = RuntimeEventPublisher(RuntimeEventBus())
        publisher.record_turn_latency("k1", 5)
        publisher.record_turn_runtime("k1", object())
        publisher.clear_turn("k1")
        assert "k1" not in publisher._turn_latency_ms
        assert "k1" not in publisher._turn_runtime

    def test_ensure_publisher_creates_lazily_and_reuses(self):
        owner = type("Owner", (), {})()
        publisher = ensure_runtime_event_publisher(owner)
        assert isinstance(publisher, RuntimeEventPublisher)
        assert isinstance(owner.runtime_events, RuntimeEventBus)
        assert owner.runtime_event_publisher is publisher
        assert ensure_runtime_event_publisher(owner) is publisher


# ---------------------------------------------------------------------------
# bus/progress.py
# ---------------------------------------------------------------------------


class TestBusProgressCallback:
    @pytest.mark.asyncio
    async def test_publishes_progress_event(self):
        bus = MessageBus()
        msg = InboundMessage(content="hi", chat_id="c1")
        callback = build_bus_progress_callback(bus, msg)
        await callback("working on it")
        outbound = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
        assert isinstance(outbound.event, ProgressEvent)
        assert outbound.event.content == "working on it"
        assert outbound.channel == "cli"
        assert outbound.chat_id == "c1"
        assert outbound.content == "working on it"

    @pytest.mark.asyncio
    async def test_publishes_with_flags(self):
        bus = MessageBus()
        msg = InboundMessage(content="hi", chat_id="c1")
        callback = build_bus_progress_callback(bus, msg)
        await callback("tool", tool_hint=True, reasoning=True)
        outbound = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
        assert outbound.event.tool_hint is True
        assert outbound.event.reasoning_delta is True


# ---------------------------------------------------------------------------
# provider on_retry_wait 心跳
# ---------------------------------------------------------------------------


class _FlakyProvider(LLMProvider):
    """覆盖 *fail_count* 次抛瞬态错误，随后返回正常响应。"""

    def __init__(self, fail_count: int = 1):
        super().__init__()
        self.fail_count = fail_count
        self.calls = 0

    @property
    def model(self) -> str:
        return "mock-model"

    async def chat(
        self,
        messages,
        tools=None,
        model=None,
        temperature=0.7,
        max_tokens=4096,
    ):
        self.calls += 1
        if self.calls <= self.fail_count:
            raise asyncio.TimeoutError("transient")
        return LLMResponse(content="recovered", finish_reason="stop")


class _FlakyStreamProvider(LLMProvider):
    """覆盖 chat_stream：前 N 次抛瞬态错误（基类重试路径）。"""

    def __init__(self, fail_count: int = 1):
        super().__init__()
        self.fail_count = fail_count
        self.calls = 0

    @property
    def model(self) -> str:
        return "mock-stream"

    async def chat(
        self,
        messages,
        tools=None,
        model=None,
        temperature=0.7,
        max_tokens=4096,
    ):
        self.calls += 1
        if self.calls <= self.fail_count:
            raise asyncio.TimeoutError("transient")
        return LLMResponse(content="recovered", finish_reason="stop")

    async def chat_stream(
        self,
        messages,
        tools=None,
        model=None,
        temperature=0.7,
        max_tokens=4096,
        on_content_delta=None,
    ):
        self.calls += 1
        if self.calls <= self.fail_count:
            raise asyncio.TimeoutError("transient")
        if on_content_delta:
            await on_content_delta("recovered")
        return LLMResponse(content="recovered", finish_reason="stop")


class TestProviderRetryWaitHeartbeat:
    @pytest.mark.asyncio
    async def test_chat_with_retry_emits_heartbeat(self):
        provider = _FlakyProvider(fail_count=1)
        waits: list[str] = []

        async def _on_wait(text: str) -> None:
            waits.append(text)

        response = await provider.chat_with_retry(
            initial_messages=[{"role": "user", "content": "hi"}],
            retry_config=RetryConfig(max_retries=3, base_delay=0.001, max_delay=0.002),
            on_retry_wait=_on_wait,
        )
        assert response.content == "recovered"
        assert provider.calls == 2
        assert len(waits) >= 1
        assert "attempt 1" in waits[0]
        assert "retry in" in waits[0].lower()

    @pytest.mark.asyncio
    async def test_chat_stream_with_retry_emits_heartbeat(self):
        provider = _FlakyStreamProvider(fail_count=1)
        waits: list[str] = []

        async def _on_wait(text: str) -> None:
            waits.append(text)

        response = await provider.chat_stream_with_retry(
            initial_messages=[{"role": "user", "content": "hi"}],
            retry_config=RetryConfig(max_retries=3, base_delay=0.001, max_delay=0.002),
            on_retry_wait=_on_wait,
        )
        assert response.content == "recovered"
        assert len(waits) >= 1


# ---------------------------------------------------------------------------
# loop 集成：运行时事件 + outbound typed 事件
# ---------------------------------------------------------------------------


def _make_loop(tmp_path, provider) -> AgentLoop:
    """构造带 mock provider 的完整 AgentLoop（面向 pytest tmp_path）。"""

    bus = MessageBus()
    registry = ToolRegistry()
    registry.register(EchoTool())
    session_manager = SessionManager(workspace=str(tmp_path))
    context_builder = ContextBuilder(workspace=str(tmp_path))
    memory = MemoryStore(workspace=str(tmp_path))
    return AgentLoop(
        bus=bus, provider=provider, registry=registry,
        session_manager=session_manager, context_builder=context_builder,
        memory=memory, identity="You are a test bot.",
        replay_budget=10_000,
    )


class _SimpleProvider(LLMProvider):
    def __init__(self, content: str = "Hello world!"):
        super().__init__()
        self._content = content

    @property
    def model(self) -> str:
        return "mock-model"

    async def chat(
        self,
        messages,
        tools=None,
        model=None,
        temperature=0.7,
        max_tokens=4096,
    ):
        return LLMResponse(content=self._content, finish_reason="stop")


class _ToolFirstProvider(LLMProvider):
    """第一轮触发 echo 工具，第二轮给最终回复。"""

    def __init__(self):
        super().__init__()
        self.tool_round = True

    @property
    def model(self) -> str:
        return "mock-tools"

    async def chat(
        self,
        messages,
        tools=None,
        model=None,
        temperature=0.7,
        max_tokens=4096,
    ):
        if self.tool_round:
            self.tool_round = False
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(id="call_1", name="echo", arguments={"text": "hi"})
                ],
                finish_reason="tool_calls",
            )
        return LLMResponse(content="done", finish_reason="stop")


async def _consume_until(
    bus: MessageBus, predicate, timeout: float = 2.0
) -> OutboundMessage:
    """消费 outbound 直到 predicate(msg) 为真，超时抛 asyncio.TimeoutError。"""

    async def _scan():
        while True:
            msg = await bus.consume_outbound()
            if predicate(msg):
                return msg

    return await asyncio.wait_for(_scan(), timeout=timeout)


async def _started_loop(loop: AgentLoop):
    """启动 loop.run() 任务并返回 (bus, task)。"""

    task = asyncio.create_task(loop.run())
    return loop.bus, task


class TestLoopRuntimeEvents:
    @pytest.mark.asyncio
    async def test_full_turn_emits_lifecycle_events(self, tmp_path):
        loop = _make_loop(tmp_path, _SimpleProvider())
        bus = loop.bus
        lifecycle: list[str] = []

        async def _on_event(event) -> None:
            if isinstance(event, SessionTurnStarted):
                lifecycle.append(f"started:{event.context.session_key}")
            elif isinstance(event, TurnRunStatusChanged):
                lifecycle.append(f"status:{event.status}")
            elif isinstance(event, TurnCompleted):
                lifecycle.append(f"completed:{event.latency_ms is not None}")

        loop.runtime_events.subscribe(_on_event)
        task = asyncio.create_task(loop.run())
        await bus.publish_inbound(InboundMessage(content="hi", chat_id="c1"))
        # 等最终响应并清理 outbound
        await _consume_until(bus, lambda m: m.content and m.event is None)
        # idle 事件由 _dispatch finally 派发，等待若干轮事件循环
        for _ in range(100):
            if "status:idle" in lifecycle:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert "started:c1" in lifecycle
        assert "status:running" in lifecycle
        assert "status:idle" in lifecycle
        assert any(s.startswith("completed:") for s in lifecycle)

    @pytest.mark.asyncio
    async def test_final_response_carries_streamed_event(self, tmp_path):
        loop = _make_loop(tmp_path, _SimpleProvider())
        bus = loop.bus
        task = asyncio.create_task(loop.run())
        await bus.publish_inbound(InboundMessage(content="hi", chat_id="c1"))
        final = await _consume_until(
            bus,
            lambda m: m.event is not None and isinstance(m.event, StreamedResponseEvent),
        )
        assert final.content == "Hello world!"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


class TestStreamResumingSemantics:
    """step32：runner 关流语义——工具执行/注入续跑期间流保持存活。

    ``on_stream_end(resuming=True)`` 不发 finished 收尾标记，只有真正收尾
    （resuming=False）才发。全部使用 mock provider，无真实 API 调用。
    """

    @staticmethod
    async def _consume_until_done(
        bus: MessageBus,
    ) -> tuple[list[StreamEndEvent], OutboundMessage]:
        """消费 outbound 直到最终消息（挂 StreamedResponseEvent）。

        Returns:
            (StreamEndEvent 序列, 最终消息)。
        """
        ends: list[StreamEndEvent] = []
        while True:
            msg = await asyncio.wait_for(bus.consume_outbound(), timeout=3.0)
            if isinstance(msg.event, StreamEndEvent):
                ends.append(msg.event)
            elif msg.event is not None and isinstance(msg.event, StreamedResponseEvent):
                return ends, msg

    @pytest.mark.asyncio
    async def test_final_end_emits_resuming_false(self, tmp_path):
        loop = _make_loop(tmp_path, _SimpleProvider())
        bus = loop.bus
        task = asyncio.create_task(loop.run())
        await bus.publish_inbound(InboundMessage(content="hi", chat_id="c1"))
        ends, final = await self._consume_until_done(bus)
        assert ends, "应有 StreamEndEvent 收尾"
        # 收尾信号必须是 resuming=False（流真正结束）
        assert ends[-1].resuming is False
        assert final.content == "Hello world!"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_tool_calls_keep_stream_alive(self, tmp_path):
        loop = _make_loop(tmp_path, _ToolFirstProvider())
        bus = loop.bus
        task = asyncio.create_task(loop.run())
        await bus.publish_inbound(InboundMessage(content="call tool", chat_id="c1"))
        ends, final = await self._consume_until_done(bus)
        # 工具执行紧随模型响应：期间出现 resuming=True 的续流段
        assert any(e.resuming for e in ends), ends
        # 最终收尾仍是 resuming=False
        assert ends[-1].resuming is False
        assert final.content == "done"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


class TestLoopProgressOutbound:
    @pytest.mark.asyncio
    async def test_tool_run_publishes_progress_event(self, tmp_path):
        loop = _make_loop(tmp_path, _ToolFirstProvider())
        bus = loop.bus
        task = asyncio.create_task(loop.run())
        await bus.publish_inbound(InboundMessage(content="call tool", chat_id="c1"))
        match = await _consume_until(
            bus,
            lambda m: isinstance(m.event, ProgressEvent) and "echo" in m.event.content,
        )
        assert match.event.tool_hint is False
        # 清理剩余 outbound（最终响应）
        async def _drain() -> OutboundMessage | None:
            result = None
            while True:
                try:
                    m = await asyncio.wait_for(bus.consume_outbound(), timeout=0.5)
                except asyncio.TimeoutError:
                    return result
                result = m

        await _drain()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


class TestLoopRetryWaitOutbound:
    @pytest.mark.asyncio
    async def test_retry_wait_event_published(self, tmp_path):
        loop = _make_loop(tmp_path, _FlakyProvider(fail_count=1))
        bus = loop.bus
        task = asyncio.create_task(loop.run())
        await bus.publish_inbound(InboundMessage(content="hi", chat_id="c1"))
        # 首次请求失败会重试（基类 backoff），重试等待期间发布 RetryWaitEvent
        match = await _consume_until(
            bus,
            lambda m: isinstance(m.event, RetryWaitEvent),
            timeout=3.0,
        )
        assert "retry in" in match.event.content
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


# ---------------------------------------------------------------------------
# manager 路由 + cli 通道语义
# ---------------------------------------------------------------------------


class TestManagerTypedEventRouting:
    @pytest.mark.asyncio
    async def test_stream_end_routes_to_send_delta(self):
        bus = MessageBus()
        manager = ChannelManager(config={"cli": {"enabled": True, "streaming": True}}, bus=bus)
        channel = manager.get_channel("cli")
        assert isinstance(channel, CliChannel)
        received: list[tuple] = []

        async def _record_send_delta(
            chat_id,
            delta,
            metadata=None,
            *,
            stream_id=None,
            stream_end=False,
            resuming=False,
        ):
            received.append((chat_id, delta, stream_end, resuming))

        channel.send_delta = _record_send_delta
        await bus.publish_outbound(
            outbound_message_for_event(
                channel="cli", chat_id="c1", event=StreamEndEvent(resuming=True),
            )
        )
        task = asyncio.create_task(manager._dispatch_outbound())
        for _ in range(100):
            if received:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        assert received
        _, _, stream_end, resuming = received[0]
        assert stream_end is True
        assert resuming is True

    @pytest.mark.asyncio
    async def test_progress_routes_to_send(self):
        bus = MessageBus()
        manager = ChannelManager(config={"cli": {"enabled": True}}, bus=bus)
        channel = manager.get_channel("cli")
        sent: list[OutboundMessage] = []

        async def _record_send(msg: OutboundMessage) -> None:
            sent.append(msg)

        channel.send = _record_send
        await bus.publish_outbound(
            outbound_message_for_event(
                channel="cli", chat_id="c1", event=ProgressEvent(content="busy"),
            )
        )
        task = asyncio.create_task(manager._dispatch_outbound())
        for _ in range(100):
            if sent:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        assert sent and sent[0].content == "busy"
        assert isinstance(sent[0].event, ProgressEvent)


class TestCliChannelEventSemantics:
    @pytest.mark.asyncio
    async def test_runtime_event_does_not_end_turn(self):  # noqa: D102
        channel = CliChannel(config={"enabled": True})
        channel._turn_done = asyncio.Event()
        await channel.send(OutboundMessage(content="busy", event=ProgressEvent(content="busy")))
        assert channel._turn_done.is_set() is False

    @pytest.mark.asyncio
    async def test_final_message_ends_turn(self):  # noqa: D102
        channel = CliChannel(config={"enabled": True})
        channel._turn_done = asyncio.Event()
        await channel.send(OutboundMessage(
            content="final", metadata={"stop_reason": "stop"},
        ))
        assert channel._turn_done.is_set()

    def test_legacy_stream_delta_still_subclass(self):  # noqa: D102
        msg = StreamDeltaEvent(content="d", finished=False)
        assert isinstance(msg, OutboundMessage)
        assert msg.event is None
