from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import Any

from step21.bus import MessageBus
from step21.autocompact import AutoCompact
from step21.command import CommandContext, CommandRouter, register_builtin_commands
from step21.consolidation import Consolidator
from step21.context import ContextBuilder
from step21.events import InboundMessage, OutboundMessage, StreamDeltaEvent
from step21.goal_state import goal_state_runtime_lines, sustained_goal_active
from step21.hook import AgentHook, AgentHookContext, CompositeHook
from step21.llm import LLMResponse, Runtime, ToolCallRequest
from step21.memory import MemoryStore
from step21.pairing import PairingStore
from step21.runner import AgentRunResult, AgentRunSpec, AgentRunner
from step21.session import Session, SessionManager
from step21.subagent import SubagentManager
from step21.tool import ToolRegistry
from step21.context import ToolContext
from step21.loader import ToolLoader


class TurnState(Enum):
    RESTORE = auto()
    COMPACT = auto()
    COMMAND = auto()
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
    _PENDING_USER_TURN_KEY = "pending_user_turn"
    _TRANSITIONS: dict[tuple[TurnState, str], TurnState] = {
        (TurnState.RESTORE, "ok"): TurnState.COMPACT,
        (TurnState.COMPACT, "ok"): TurnState.COMMAND,
        (TurnState.COMMAND, "dispatch"): TurnState.BUILD,
        (TurnState.COMMAND, "shortcut"): TurnState.DONE,
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
        session_ttl_minutes: int = 0,
        pairing: PairingStore | None = None,
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
        self.pairing = pairing
        self.commands = CommandRouter()
        register_builtin_commands(self.commands)
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
        self._goal_continue_message = (
            "You have an active sustained goal. "
            "Continue working toward the objective using your tools, "
            "or call update_goal with action='complete' if the work is done."
        )
        self.auto_compact = AutoCompact(
            session_manager, self.consolidator, session_ttl_minutes
        )

    def _schedule_background(self, coro: Any) -> None:
        asyncio.create_task(coro)

    async def run(self) -> None:
        self.running = True
        while self.running:
            msg = await self.bus.consume_inbound()
            self.auto_compact.check_expired(
                self._schedule_background,
                lambda: self.runtime,
                active_session_keys=set(self._pending_queues),
            )
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
        if self._restore_pending_user_turn(ctx.session):
            self.sessions.save(ctx.session)
        return "ok"

    async def _state_compact(self, ctx: TurnContext) -> str:
        ctx.session, pending = self.auto_compact.prepare_session(
            ctx.session, ctx.session_key
        )
        ctx.summary = pending
        await self.consolidator.maybe_consolidate_by_tokens(
            ctx.session, runtime=self.runtime,
        )
        if ctx.summary is None:
            meta = ctx.session.metadata.get("_last_summary")
            ctx.summary = meta.get("text") if isinstance(meta, dict) else None
        return "ok"

    async def _state_command(self, ctx: TurnContext) -> str:
        raw = ctx.msg.content.strip()
        if not raw.startswith("/"):
            return "dispatch"
        cmd_ctx = CommandContext(
            msg=ctx.msg,
            session=ctx.session,
            key=ctx.session_key,
            raw=raw,
            loop=self,
        )
        result = await self.commands.dispatch(cmd_ctx)
        if result is None:
            return "dispatch"
        result.channel = ctx.msg.channel
        result.chat_id = ctx.msg.chat_id
        ctx.outbound = result
        return "shortcut"

    async def _state_build(self, ctx: TurnContext) -> str:
        ctx.history = ctx.session.get_history(max_messages=50, max_tokens=self.replay_budget)
        goal_lines = goal_state_runtime_lines(ctx.session.metadata)
        identity = self.identity
        if goal_lines:
            identity = identity + "\n\n" + "\n".join(goal_lines)
        ctx.session.add_message("user", ctx.msg.content)
        self._mark_pending_user_turn(ctx.session)
        self.sessions.save(ctx.session)
        ctx.initial_messages = self.context.build_messages(
            current_message=ctx.msg.content,
            history=ctx.history,
            identity=identity,
            session_summary=ctx.summary,
        )
        return "ok"

    async def _state_run(self, ctx: TurnContext) -> str:
        session_key = ctx.session_key

        tool_ctx = ToolContext(
            config=None, workspace="",
            bus=self.bus, subagent_manager=self.subagents,
            sessions=self.sessions,
        )
        ToolLoader().load(tool_ctx, self.registry, scope="core")

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
        skip = 2 + len(ctx.history)
        ctx.session.import_messages(ctx.result.messages[skip:])
        self._clear_pending_user_turn(ctx.session)
        ctx.session.enforce_file_cap(
            on_archive=lambda chunk: self.memory.raw_archive(
                chunk, session_key=ctx.session_key
            )
        )
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

    def _mark_pending_user_turn(self, session: Session) -> None:
        session.metadata[self._PENDING_USER_TURN_KEY] = True

    def _clear_pending_user_turn(self, session: Session) -> None:
        session.metadata.pop(self._PENDING_USER_TURN_KEY, None)

    def _restore_pending_user_turn(self, session: Session) -> bool:
        """Close a turn that only persisted the user message before crashing."""
        if not session.metadata.get(self._PENDING_USER_TURN_KEY):
            return False

        if session.messages and session.messages[-1].get("role") == "user":
            session.messages.append(
                {
                    "role": "assistant",
                    "content": "Error: Task interrupted before a response was generated.",
                    "timestamp": datetime.now().isoformat(),
                }
            )
            session.updated_at = datetime.now().isoformat()

        self._clear_pending_user_turn(session)
        return True

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
