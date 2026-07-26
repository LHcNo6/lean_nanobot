from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any

from step12.bus import MessageBus
from step12.consolidation import Consolidator
from step12.context import ContextBuilder
from step12.events import InboundMessage, OutboundMessage, StreamDeltaEvent
from step12.hook import AgentHook, AgentHookContext, CompositeHook
from step12.runner import AgentRunResult, AgentRunSpec, AgentRunner
from step12.session import Session, SessionManager
from step12.tool import ToolRegistry


class TurnState(Enum):
    RESTORE = auto()
    COMPACT = auto()
    BUILD = auto()
    RUN = auto()
    SAVE = auto()
    RESPOND = auto()
    DONE = auto()


@dataclass
class TurnContext:
    msg: InboundMessage
    session_key: str
    state: TurnState = TurnState.RESTORE
    session: Session | None = None
    summary: str | None = None
    history: list[dict[str, Any]] = field(default_factory=list)
    initial_messages: list[dict[str, Any]] = field(default_factory=list)
    result: AgentRunResult | None = None
    outbound: OutboundMessage | None = None


class StreamPublishingHook(AgentHook):
    def __init__(self, bus: MessageBus, chat_id: str, channel: str = "cli", session_key: str | None = None) -> None:
        super().__init__()
        self.bus = bus
        self.chat_id = chat_id
        self.channel = channel
        self.session_key = session_key

    async def on_stream(self, ctx: AgentHookContext, delta: str) -> None:
        if not delta:
            return
        await self.bus.publish_outbound(StreamDeltaEvent(
            content=delta,
            channel=self.channel,
            chat_id=self.chat_id,
            finished=False,
            session_key=self.session_key,
        ))

    async def on_stream_end(self, ctx: AgentHookContext) -> None:
        await self.bus.publish_outbound(StreamDeltaEvent(
            content="",
            channel=self.channel,
            chat_id=self.chat_id,
            finished=True,
            session_key=self.session_key,
        ))


class AgentLoop:
    _TRANSITIONS: dict[tuple[TurnState, str], TurnState] = {
        (TurnState.RESTORE, "ok"): TurnState.COMPACT,
        (TurnState.COMPACT, "ok"): TurnState.BUILD,
        (TurnState.BUILD, "ok"): TurnState.RUN,
        (TurnState.RUN, "ok"): TurnState.SAVE,
        (TurnState.SAVE, "ok"): TurnState.RESPOND,
        (TurnState.RESPOND, "ok"): TurnState.DONE,
    }

    def __init__(
        self,
        bus: MessageBus,
        provider: Any,
        registry: ToolRegistry,
        session_manager: SessionManager,
        context_builder: ContextBuilder,
        consolidator: Consolidator,
        identity: str,
        replay_budget: int,
        hooks: list[AgentHook] | None = None,
    ) -> None:
        self.bus = bus
        self.provider = provider
        self.registry = registry
        self.sessions = session_manager
        self.context = context_builder
        self.consolidator = consolidator
        self.identity = identity
        self.replay_budget = replay_budget
        self.hooks = list(hooks) if hooks else []
        self.running = False
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._runner = AgentRunner()

    async def run(self) -> None:
        self.running = True
        while self.running:
            msg = await self.bus.consume_inbound()
            asyncio.create_task(self._dispatch(msg))

    def stop(self) -> None:
        self.running = False

    async def _dispatch(self, msg: InboundMessage) -> None:
        session_key = msg.session_key or msg.chat_id
        lock = self._session_locks.setdefault(session_key, asyncio.Lock())
        async with lock:
            response = await self._process_message(msg, session_key)
            if response is not None:
                await self.bus.publish_outbound(response)

    async def _process_message(
        self, msg: InboundMessage, session_key: str
    ) -> OutboundMessage | None:
        ctx = TurnContext(msg=msg, session_key=session_key)
        while ctx.state != TurnState.DONE:
            handler_name = f"_state_{ctx.state.name.lower()}"
            handler = getattr(self, handler_name)
            try:
                event = await handler(ctx)
            except Exception as exc:
                ctx.outbound = OutboundMessage(
                    content=f"Error: {exc}",
                    metadata={"stop_reason": "error"},
                )
                break
            next_state = self._TRANSITIONS.get((ctx.state, event))
            if next_state is None:
                ctx.outbound = OutboundMessage(
                    content=f"Unexpected event '{event}' in state {ctx.state.name}",
                    metadata={"stop_reason": "error"},
                )
                break
            ctx.state = next_state
        return ctx.outbound

    async def _state_restore(self, ctx: TurnContext) -> str:
        ctx.session = self.sessions.get_or_create(ctx.session_key)
        return "ok"

    async def _state_compact(self, ctx: TurnContext) -> str:
        ctx.summary = await self.consolidator.maybe_consolidate(
            ctx.session, max_tokens=self.replay_budget, model=self.provider.model,
        )
        return "ok"

    async def _state_build(self, ctx: TurnContext) -> str:
        ctx.history = ctx.session.get_history(max_messages=50, max_tokens=self.replay_budget)
        ctx.initial_messages = self.context.build_messages(
            current_message=ctx.msg.content,
            history=ctx.history,
            identity=self.identity,
            session_summary=ctx.summary,
        )
        return "ok"

    async def _state_run(self, ctx: TurnContext) -> str:
        hooks = list(self.hooks)
        hooks.append(StreamPublishingHook(
            bus=self.bus,
            chat_id=ctx.msg.chat_id,
            channel=ctx.msg.channel,
            session_key=ctx.session_key,
        ))
        hook: AgentHook | None = CompositeHook(hooks) if len(hooks) > 1 else hooks[0]
        spec = AgentRunSpec(
            initial_messages=ctx.initial_messages,
            tools=self.registry,
            provider=self.provider,
            max_iterations=5,
            hook=hook,
            session_key=ctx.session_key,
        )
        ctx.result = await self._runner.run(spec)
        return "ok"

    async def _state_save(self, ctx: TurnContext) -> str:
        if ctx.result is None:
            return "ok"
        skip = 1 + len(ctx.history)
        ctx.session.import_messages(ctx.result.messages[skip:])
        self.sessions.save(ctx.session)
        return "ok"

    async def _state_respond(self, ctx: TurnContext) -> str:
        if ctx.result is None:
            ctx.outbound = OutboundMessage(content="", metadata={"stop_reason": "empty"})
            return "ok"
        ctx.outbound = OutboundMessage(
            content=ctx.result.final_content or "",
            metadata={
                "stop_reason": ctx.result.stop_reason,
                "tokens": f"{ctx.result.total_prompt_tokens}+{ctx.result.total_completion_tokens}",
            },
        )
        return "ok"
