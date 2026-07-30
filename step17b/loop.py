from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import Any

from step17b.bus import MessageBus
from step17b.consolidation import Consolidator
from step17b.context import ContextBuilder
from step17b.events import InboundMessage, OutboundMessage, StreamDeltaEvent
from step17b.goal_state import goal_state_runtime_lines, sustained_goal_active
from step17b.hook import AgentHook, AgentHookContext, CompositeHook
from step17b.llm import LLMResponse, Runtime, ToolCallRequest
from step17b.memory import MemoryStore
from step17b.runner import AgentRunResult, AgentRunSpec, AgentRunner
from step17b.session import Session, SessionManager
from step17b.subagent import SubagentManager
from step17b.tool import ToolRegistry
from step17b.tools.long_task import CreateGoalTool, UpdateGoalTool
from step17b.tools.spawn import SpawnTool


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
            content=delta, channel=self.channel, chat_id=self.chat_id,
            finished=False, session_key=self.session_key,
        ))

    async def on_stream_end(self, ctx: AgentHookContext) -> None:
        await self.bus.publish_outbound(StreamDeltaEvent(
            content="", channel=self.channel, chat_id=self.chat_id,
            finished=True, session_key=self.session_key,
        ))

    def wants_streaming(self) -> bool:
        return True


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
        memory: MemoryStore,
        identity: str,
        replay_budget: int,
        subagent_manager: SubagentManager | None = None,
        hooks: list[AgentHook] | None = None,
    ) -> None:
        self.bus = bus
        self.provider = provider
        self.registry = registry
        self.sessions = session_manager
        self.context = context_builder
        self.memory = memory
        self.identity = identity
        self.replay_budget = replay_budget
        self.subagents = subagent_manager
        self.hooks = list(hooks) if hooks else []
        self.running = False
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._pending_queues: dict[str, asyncio.Queue[InboundMessage]] = {}
        self._runner = AgentRunner()
        self.runtime = Runtime(
            context_window_tokens=replay_budget,
            max_tokens=4096,
            provider=provider,
            model=getattr(provider, "model", None),
        )
        self.consolidator = Consolidator(
            store=memory,
            sessions=session_manager,
            build_messages=context_builder.build_messages,
            get_tool_definitions=registry.get_definitions,
            provider=provider,
        )
        self._create_goal_tool = CreateGoalTool(sessions=session_manager)
        self._update_goal_tool = UpdateGoalTool(sessions=session_manager)
        self._spawn_tool = SpawnTool(manager=subagent_manager)
        self._goal_continue_message = (
            "You have an active sustained goal. "
            "Continue working toward the objective using your tools, "
            "or call update_goal with action='complete' if the work is done."
        )

    def _schedule_background(self, coro: Any) -> None:
        asyncio.create_task(coro)

    async def run(self) -> None:
        self.running = True
        while self.running:
            msg = await self.bus.consume_inbound()
            asyncio.create_task(self._dispatch(msg))

    def stop(self) -> None:
        self.running = False

    def _get_or_create_queue(self, session_key: str) -> asyncio.Queue[InboundMessage]:
        if session_key not in self._pending_queues:
            self._pending_queues[session_key] = asyncio.Queue(maxsize=20)
        return self._pending_queues[session_key]

    async def _dispatch(self, msg: InboundMessage) -> None:
        session_key = msg.session_key_override or msg.session_key or msg.chat_id
        lock = self._session_locks.setdefault(session_key, asyncio.Lock())
        if lock.locked():
            await self._get_or_create_queue(session_key).put(msg)
            return
        async with lock:
            response = await self._process_message(msg, session_key)
            if response is not None:
                await self.bus.publish_outbound(response)
        await self._drain_leftover(session_key)

    async def _drain_leftover(self, session_key: str) -> None:
        queue = self._pending_queues.get(session_key)
        if queue and not queue.empty():
            try:
                msg = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            await self.bus.publish_inbound(msg)

    async def _process_message(self, msg: InboundMessage, session_key: str) -> OutboundMessage | None:
        ctx = TurnContext(msg=msg, session_key=session_key)
        while ctx.state != TurnState.DONE:
            handler_name = f"_state_{ctx.state.name.lower()}"
            handler = getattr(self, handler_name)
            try:
                event = await handler(ctx)
            except Exception as exc:
                ctx.outbound = OutboundMessage(
                    content=f"Error: {exc}", metadata={"stop_reason": "error"},
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
        await self.consolidator.maybe_consolidate_by_tokens(
            ctx.session, runtime=self.runtime,
        )
        meta = ctx.session.metadata.get("_last_summary")
        ctx.summary = meta.get("text") if isinstance(meta, dict) else None
        return "ok"

    async def _state_build(self, ctx: TurnContext) -> str:
        ctx.history = ctx.session.get_history(max_messages=50, max_tokens=self.replay_budget)
        goal_lines = goal_state_runtime_lines(ctx.session.metadata)
        identity = self.identity
        if goal_lines:
            identity = identity + "\n\n" + "\n".join(goal_lines)
        ctx.initial_messages = self.context.build_messages(
            current_message=ctx.msg.content,
            history=ctx.history,
            identity=identity,
            session_summary=ctx.summary,
        )
        return "ok"

    async def _state_run(self, ctx: TurnContext) -> str:
        session_key = ctx.session_key
        self._create_goal_tool.set_session_key(session_key)
        self._update_goal_tool.set_session_key(session_key)

        self.registry.register(self._spawn_tool)
        self.registry.register(self._create_goal_tool)
        self.registry.register(self._update_goal_tool)

        hooks = list(self.hooks)
        hooks.append(StreamPublishingHook(
            bus=self.bus, chat_id=ctx.msg.chat_id,
            channel=ctx.msg.channel, session_key=session_key,
        ))
        hook = CompositeHook(hooks) if len(hooks) > 1 else hooks[0]

        rounds = 0
        if ctx.session:
            rounds = ctx.session.metadata.get("_goal_continuation_rounds", 0)

        spec = AgentRunSpec(
            initial_messages=ctx.initial_messages,
            tools=self.registry,
            provider=self.provider,
            max_iterations=5,
            hook=hook,
            session_key=session_key,
            goal_active_predicate=lambda: sustained_goal_active(ctx.session.metadata) if ctx.session else False,
            goal_continue_message=self._goal_continue_message,
            goal_continuation_rounds=rounds,
        )
        ctx.result = await self._runner.run(spec)

        new_rounds = spec.goal_continuation_rounds
        if ctx.session and new_rounds > rounds:
            ctx.session.metadata["_goal_continuation_rounds"] = new_rounds
        return "ok"

    async def _state_save(self, ctx: TurnContext) -> str:
        if ctx.result is None:
            return "ok"
        skip = 1 + len(ctx.history)
        ctx.session.import_messages(ctx.result.messages[skip:])
        self.sessions.save(ctx.session)
        self._schedule_background(
            self.consolidator.maybe_consolidate_by_tokens(
                ctx.session, runtime=self.runtime,
            )
        )
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

    async def run_dream(self, tools: ToolRegistry | None = None) -> AgentRunResult | None:
        result = self.memory.build_dream_prompt(max_entries=20)
        if result is None:
            return None
        prompt, last_cursor = result
        dream_key = f"dream:{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": prompt}],
            tools=tools or self.registry,
            provider=self.provider,
            max_iterations=15,
            session_key=dream_key,
        )
        try:
            run_result = await self._runner.run(spec)
            self.memory.set_last_dream_cursor(last_cursor)
            return run_result
        except Exception:
            return None
