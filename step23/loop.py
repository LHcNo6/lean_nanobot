from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import Any

from step23.bus import MessageBus
from step23.autocompact import AutoCompact
from step23.command import CommandContext, CommandRouter, register_builtin_commands
from step23.consolidation import Consolidator
from step23.context import ContextBuilder
from step23.events import InboundMessage, OutboundMessage, StreamDeltaEvent
from step23.goal_state import goal_state_runtime_lines, sustained_goal_active
from step23.hook import AgentHook, AgentHookContext, CompositeHook
from step23.llm import LLMResponse, LLMRuntime, ToolCallRequest
from step23.memory import MemoryStore
from step23.pairing import PairingStore
from step23.runner import (
    AgentRunResult,
    AgentRunSpec,
    AgentRunner,
    _MAX_INJECTIONS_PER_TURN,
)
from step23.session import Session, SessionManager
from step23.subagent import SubagentManager
from step23.tool import ToolRegistry
from step23.context import ToolContext
from step23.loader import ToolLoader


class TurnState(Enum):
    RESTORE = auto()
    COMPACT = auto()
    COMMAND = auto()
    BUILD = auto()
    RUN = auto()
    SAVE = auto()
    RESPOND = auto()
    DONE = auto()


_REPLAY_SAFETY_BUFFER = 128  # 从 context window 反推 replay budget 时的安全余量


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
    turn_id: str | None = None
    runtime: LLMRuntime | None = None
    on_progress: Any | None = None
    on_stream: Any | None = None
    on_stream_end: Any | None = None
    pending_queue: asyncio.Queue[Any] | None = None


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
        replay_budget: int | None = None,
        runtime: LLMRuntime | None = None,
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
        self.subagents = subagent_manager
        self.hooks = list(hooks) if hooks else []
        self.pairing = pairing
        self.commands = CommandRouter()
        register_builtin_commands(self.commands)
        self.running = False
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._pending_queues: dict[str, asyncio.Queue[InboundMessage]] = {}
        self._runner = AgentRunner()
        if runtime is not None:
            self.runtime = runtime
        elif replay_budget is not None:
            self.runtime = LLMRuntime.capture(
                provider=provider,
                model=getattr(provider, "model", None),
                context_window_tokens=max(replay_budget, 0),
                max_tokens=4096,
            )
        else:
            raise ValueError("AgentLoop requires replay_budget or runtime")
        if replay_budget is not None:
            self.replay_budget = replay_budget
        else:
            self.replay_budget = (
                self.runtime.context_window_tokens
                - self.runtime.generation.max_tokens
                - _REPLAY_SAFETY_BUFFER
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
            # Session busy: route the message into its pending queue for
            # mid-turn injection instead of creating a competing task.
            await self._get_or_create_queue(session_key).put(msg)
            return
        pending: asyncio.Queue[InboundMessage] | None = None
        try:
            async with lock:
                pending = self._get_or_create_queue(session_key)
                response = await self._process_message(
                    msg, session_key, pending_queue=pending, runtime=self.runtime,
                )
                if response is not None:
                    await self.bus.publish_outbound(response)
        finally:
            # Only the task that owns the session lock may remove the queue;
            # anything still pending is re-published so it is processed as a
            # fresh inbound message rather than silently lost.
            queue = None
            if self._pending_queues.get(session_key) is pending:
                queue = self._pending_queues.pop(session_key, None)
            else:
                queue = pending
            if queue is not None:
                while True:
                    try:
                        item = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    await self.bus.publish_inbound(item)

    async def _process_message(
        self,
        msg: InboundMessage,
        session_key: str,
        *,
        pending_queue: asyncio.Queue[InboundMessage] | None = None,
        runtime: LLMRuntime | None = None,
    ) -> OutboundMessage | None:
        if msg.channel == "system":
            return await self._process_system_message(
                msg, runtime=runtime, pending_queue=pending_queue,
            )

        ctx = TurnContext(
            msg=msg,
            session_key=session_key,
            turn_id=f"{session_key}:{time.time_ns()}",
            runtime=runtime or self.runtime,
            pending_queue=pending_queue,
        )
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

    def _build_agent_spec(
        self,
        msg: InboundMessage,
        session_key: str,
        session: Session | None,
        initial_messages: list[dict[str, Any]],
        *,
        injection_callback: Callable[..., Awaitable[list[dict]]] | None = None,
    ) -> AgentRunSpec:
        tool_ctx = ToolContext(
            config=None, workspace="",
            bus=self.bus, subagent_manager=self.subagents,
            sessions=self.sessions, session_key=session_key,
        )
        ToolLoader().load(tool_ctx, self.registry, scope="core")

        hooks = list(self.hooks)
        hooks.append(StreamPublishingHook(
            bus=self.bus, chat_id=msg.chat_id,
            channel=msg.channel, session_key=session_key,
        ))
        hook = CompositeHook(hooks) if len(hooks) > 1 else hooks[0]

        rounds = session.metadata.get("_goal_continuation_rounds", 0) if session else 0
        return AgentRunSpec(
            initial_messages=initial_messages,
            tools=self.registry,
            provider=self.provider,
            max_iterations=5,
            hook=hook,
            session_key=session_key,
            injection_callback=injection_callback,
            goal_active_predicate=lambda: (
                sustained_goal_active(session.metadata) if session else False
            ),
            goal_continue_message=self._goal_continue_message,
            goal_continuation_rounds=rounds,
            model=self.runtime.model,
            temperature=self.runtime.generation.temperature,
            max_tokens=self.runtime.generation.max_tokens,
            context_window_tokens=self.runtime.context_window_tokens,
        )

    @staticmethod
    def _pending_to_user_message(pending_msg: InboundMessage) -> dict[str, Any]:
        row: dict[str, Any] = {"role": "user", "content": pending_msg.content}
        metadata = pending_msg.metadata if isinstance(pending_msg.metadata, dict) else {}
        if (
            pending_msg.sender_id == "subagent"
            and metadata.get("injected_event") == "subagent_result"
        ):
            row["injected_event"] = "subagent_result"
            task_id = metadata.get("subagent_task_id")
            if isinstance(task_id, str) and task_id:
                row["subagent_task_id"] = task_id
        return row

    def _build_injection_callback(
        self,
        pending_queue: asyncio.Queue[InboundMessage] | None,
        session_key: str,
        session: Session | None,
    ) -> Callable[..., Awaitable[list[dict]]]:
        async def _drain_pending(
            *, limit: int = _MAX_INJECTIONS_PER_TURN
        ) -> list[dict[str, Any]]:
            if pending_queue is None:
                return []
            items: list[dict[str, Any]] = []
            while len(items) < limit:
                try:
                    pending_msg = pending_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                items.append(self._pending_to_user_message(pending_msg))
            # Keep the runner loop alive so sub-agents spawned in this dispatch
            # complete in-order rather than being dispatched as separate turns.
            if (
                not items
                and session is not None
                and self.subagents is not None
                and self.subagents.get_running_count_by_session(session_key) > 0
            ):
                try:
                    pending_msg = await asyncio.wait_for(pending_queue.get(), timeout=300)
                except asyncio.TimeoutError:
                    return items
                items.append(self._pending_to_user_message(pending_msg))
                while len(items) < limit:
                    try:
                        pending_msg = pending_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    items.append(self._pending_to_user_message(pending_msg))
            return items

        return _drain_pending

    def _persist_subagent_followup(
        self, session: Session, msg: InboundMessage
    ) -> bool:
        """Persist a subagent follow-up as an assistant message before prompt assembly.

        Returns True if a new entry was appended; False if it was deduped
        (same ``subagent_task_id`` already persisted) or carries no content.
        """
        if not msg.content:
            return False
        metadata = msg.metadata if isinstance(msg.metadata, dict) else {}
        task_id = metadata.get("subagent_task_id")
        if task_id and any(
            m.get("injected_event") == "subagent_result"
            and m.get("subagent_task_id") == task_id
            for m in session.messages
        ):
            return False
        session.add_message(
            "assistant",
            msg.content,
            sender_id=msg.sender_id,
            injected_event="subagent_result",
            subagent_task_id=task_id,
        )
        return True

    async def _process_system_message(
        self,
        msg: InboundMessage,
        *,
        runtime: LLMRuntime | None = None,
        pending_queue: asyncio.Queue[InboundMessage] | None = None,
    ) -> OutboundMessage | None:
        """Process a system inbound message (e.g. a subagent announcement).

        Subagent result messages arrive on ``channel == "system"`` and flow
        through this dedicated path so their follow-up is answered inside the
        same turn instead of queuing a competing independent turn.
        """
        burstable = runtime or self.runtime
        channel, chat_id = (
            msg.chat_id.split(":", 1) if ":" in msg.chat_id else ("cli", msg.chat_id)
        )
        key = msg.session_key_override or f"{channel}:{chat_id}"

        session = self.sessions.get_or_create(key)
        if self._restore_pending_user_turn(session):
            self.sessions.save(session)
        session, pending = self.auto_compact.prepare_session(session, key)
        await self.consolidator.maybe_consolidate_by_tokens(session, runtime=burstable)

        is_subagent = msg.sender_id == "subagent"
        if is_subagent and self._persist_subagent_followup(session, msg):
            self.sessions.save(session)
        current_role = "assistant" if is_subagent else "user"

        goal_lines = goal_state_runtime_lines(session.metadata)
        identity = self.identity
        if goal_lines:
            identity = identity + "\n\n" + "\n".join(goal_lines)
        history = session.get_history(max_messages=50, max_tokens=self.replay_budget)
        initial_messages = self.context.build_messages(
            current_message="" if is_subagent else msg.content,
            history=history,
            identity=identity,
            session_summary=pending,
            current_role=current_role,
        )

        spec = self._build_agent_spec(
            msg, key, session, initial_messages,
            injection_callback=self._build_injection_callback(pending_queue, key, session),
        )
        result = await self._runner.run(spec)

        skip = 2 + len(history)
        session.import_messages(result.messages[skip:])
        self._clear_pending_user_turn(session)
        session.enforce_file_cap(
            on_archive=lambda chunk: self.memory.raw_archive(chunk, session_key=key)
        )
        self.sessions.save(session)
        self._schedule_background(
            self.consolidator.maybe_consolidate_by_tokens(session, runtime=burstable)
        )

        content = result.final_content or "Background task completed."
        return OutboundMessage(
            channel=channel,
            chat_id=chat_id,
            content=content,
            metadata={"stop_reason": result.stop_reason or ""},
        )

    async def _state_run(self, ctx: TurnContext) -> str:
        session_key = ctx.session_key

        spec = self._build_agent_spec(
            ctx.msg, session_key, ctx.session, ctx.initial_messages,
            injection_callback=self._build_injection_callback(
                ctx.pending_queue, session_key, ctx.session,
            ),
        )
        ctx.result = await self._runner.run(spec)

        rounds = ctx.session.metadata.get("_goal_continuation_rounds", 0) if ctx.session else 0
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
