from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import Any

from step25.bus import MessageBus
from step25.autocompact import AutoCompact
from step25.command import CommandContext, CommandRouter, register_builtin_commands
from step25.consolidation import Consolidator
from step25.context import ContextBuilder
from step25.events import InboundMessage, OutboundMessage, StreamDeltaEvent
from step25.goal_state import goal_state_runtime_lines, sustained_goal_active
from step25.hook import AgentHook, AgentHookContext, CompositeHook
from step25.llm import LLMResponse, LLMRuntime, ToolCallRequest
from step25.memory import MemoryStore
from step25.pairing import PairingStore
from step25.runner import (
    AgentRunResult,
    AgentRunSpec,
    AgentRunner,
    _MAX_INJECTIONS_PER_TURN,
)
from step25.session import Session, SessionManager
from step25.subagent import SubagentManager
from step25.tool import ToolRegistry
from step25.context import ToolContext
from step25.loader import ToolLoader
from step25.helpers import truncate_text


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

logger = logging.getLogger(__name__)


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
    turn_wall_started_at: float = 0.0


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
    _RUNTIME_CHECKPOINT_KEY = "runtime_checkpoint"
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
        max_tool_result_chars: int = 16_000,
        config: Any | None = None,
    ) -> None:
        self.bus = bus
        self.provider = provider
        self.registry = registry
        self.sessions = session_manager
        self.context = context_builder
        self.memory = memory
        self.identity = identity
        self.max_tool_result_chars = max_tool_result_chars
        self.config = config  # step25: 装配时的 Config（供工具上下文解析配置）
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

    @classmethod
    def from_config(
        cls,
        config: Any,
        bus: MessageBus | None = None,
        **extra: Any,
    ) -> "AgentLoop":
        """从 Config 装配 AgentLoop（对齐 nanobot `AgentLoop.from_config` 雏形）。

        Config 驱动：`make_provider(config)` 装配 provider → `LLMRuntime.capture`
        （参数来自 `resolve_preset()`）→ workspace / session_ttl_minutes /
        max_tool_result_chars 来自 `agents.defaults`。extra 可覆盖默认装配
        （provider / registry / session_manager / memory / identity 等）。
        """
        from step25.providers.factory import make_provider

        if bus is None:
            bus = MessageBus()
        defaults = config.agents.defaults
        resolved = config.resolve_preset()
        workspace = str(config.workspace_path)
        provider = extra.pop("provider", None) or make_provider(config)
        runtime = extra.pop("runtime", None) or LLMRuntime.capture(
            provider=provider,
            model=resolved.model,
            context_window_tokens=resolved.context_window_tokens,
            max_tokens=resolved.max_tokens,
            temperature=resolved.temperature,
            model_preset=defaults.model_preset,
        )
        return cls(
            bus=bus,
            provider=provider,
            registry=extra.pop("registry", None) or ToolRegistry(),
            session_manager=extra.pop("session_manager", None)
            or SessionManager(workspace=workspace),
            context_builder=extra.pop("context_builder", None)
            or ContextBuilder(workspace=workspace),
            memory=extra.pop("memory", None) or MemoryStore(workspace=workspace),
            identity=extra.pop("identity", None)
            or f"You are {defaults.bot_name}, a lightweight AI agent.",
            runtime=runtime,
            session_ttl_minutes=extra.pop("session_ttl_minutes", defaults.session_ttl_minutes),
            max_tool_result_chars=extra.pop("max_tool_result_chars", defaults.max_tool_result_chars),
            config=config,
            **extra,
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
            turn_wall_started_at=time.time(),
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
        if self._restore_runtime_checkpoint(ctx.session):
            self.sessions.save(ctx.session)
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
        checkpoint_callback: Callable[..., Awaitable[None]] | None = None,
    ) -> AgentRunSpec:
        tool_ctx = ToolContext(
            config=self.config,
            workspace=str(self.config.workspace_path) if self.config is not None else "",
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
            checkpoint_callback=checkpoint_callback,
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
        if self._restore_runtime_checkpoint(session):
            self.sessions.save(session)
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
            checkpoint_callback=self._build_checkpoint_callback(session),
        )
        turn_started_at = time.time()
        result = await self._runner.run(spec)

        skip = len(initial_messages)
        latency_ms = max(0, int((time.time() - turn_started_at) * 1000))
        self._save_turn(session, result.messages, skip, turn_latency_ms=latency_ms)
        self._clear_pending_user_turn(session)
        self._clear_runtime_checkpoint(session)
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
            checkpoint_callback=self._build_checkpoint_callback(ctx.session),
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
        latency_ms = (
            max(0, int((time.time() - ctx.turn_wall_started_at) * 1000))
            if ctx.turn_wall_started_at
            else None
        )
        self._save_turn(
            ctx.session, ctx.result.messages, skip, turn_latency_ms=latency_ms,
        )
        self._clear_pending_user_turn(ctx.session)
        self._clear_runtime_checkpoint(ctx.session)
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

    def _build_checkpoint_callback(
        self,
        session: Session | None,
    ) -> Callable[..., Awaitable[None]] | None:
        """Wire runner checkpoint emissions to session metadata persistence."""
        if session is None:
            return None

        async def _checkpoint(payload: dict[str, Any]) -> None:
            self._set_runtime_checkpoint(session, payload)

        return _checkpoint

    def _sanitize_persisted_blocks(
        self,
        content: list[dict[str, Any]],
        *,
        should_truncate_text: bool = False,
    ) -> list[dict[str, Any]]:
        """Strip volatile multimodal payloads before writing session history."""
        filtered: list[dict[str, Any]] = []
        for block in content:
            if not isinstance(block, dict):
                filtered.append(block)
                continue
            if (
                block.get("type") == "text"
                and isinstance(block.get("text"), str)
                and should_truncate_text
                and len(block["text"]) > self.max_tool_result_chars
            ):
                filtered.append({**block, "text": truncate_text(
                    block["text"], self.max_tool_result_chars,
                )})
                continue
            filtered.append(block)
        return filtered

    def _save_turn(
        self,
        session: Session,
        messages: list[dict[str, Any]],
        skip: int,
        *,
        turn_latency_ms: int | None = None,
    ) -> None:
        """Save new-turn messages into session, sanitizing malformed entries.

        Empty assistant messages are dropped, orphaned tool results (whose
        ``tool_call_id`` was never declared by an assistant tool_calls block)
        are discarded, oversized tool results are truncated and list content
        is cleaned through ``_sanitize_persisted_blocks`` so that malformed
        rows never poison persisted history.
        """
        declared_tool_call_ids = {
            str(tc["id"])
            for m in session.messages
            if m.get("role") == "assistant"
            for tc in m.get("tool_calls") or []
            if isinstance(tc, dict) and tc.get("id")
        }
        last_assistant_idx: int | None = None
        for m in messages[skip:]:
            entry = dict(m)
            role, content = entry.get("role"), entry.get("content")
            if role == "assistant" and not content and not entry.get("tool_calls"):
                continue  # skip empty assistant messages — they poison session context
            if role == "tool":
                tool_call_id = entry.get("tool_call_id")
                if not tool_call_id or str(tool_call_id) not in declared_tool_call_ids:
                    # Undeclared tool results corrupt future provider requests.
                    logger.warning(
                        "Dropping orphaned tool result %s from session %s during persistence",
                        tool_call_id or "(missing id)",
                        session.key,
                    )
                    continue
                if isinstance(content, str) and len(content) > self.max_tool_result_chars:
                    entry["content"] = truncate_text(content, self.max_tool_result_chars)
                elif isinstance(content, list):
                    filtered = self._sanitize_persisted_blocks(
                        content, should_truncate_text=True,
                    )
                    if not filtered:
                        # Preserve the tool_call/result pair after block filtering.
                        filtered = [
                            {"type": "text", "text": "[tool result omitted during persistence]"}
                        ]
                    entry["content"] = filtered
            elif role == "user":
                if isinstance(content, list):
                    filtered = self._sanitize_persisted_blocks(content)
                    if not filtered:
                        continue
                    entry["content"] = filtered
            entry.setdefault("timestamp", datetime.now().isoformat())
            session.messages.append(entry)
            if role == "assistant":
                last_assistant_idx = len(session.messages) - 1
                declared_tool_call_ids.update(
                    str(tc["id"])
                    for tc in entry.get("tool_calls") or []
                    if isinstance(tc, dict) and tc.get("id")
                )
        if turn_latency_ms is not None and last_assistant_idx is not None:
            session.messages[last_assistant_idx]["latency_ms"] = int(turn_latency_ms)
        session.updated_at = datetime.now().isoformat()

    def _set_runtime_checkpoint(self, session: Session, payload: dict[str, Any]) -> None:
        """Persist the latest in-flight turn state into session metadata."""
        session.metadata[self._RUNTIME_CHECKPOINT_KEY] = payload
        self.sessions.save(session)

    def _clear_runtime_checkpoint(self, session: Session) -> None:
        if self._RUNTIME_CHECKPOINT_KEY in session.metadata:
            session.metadata.pop(self._RUNTIME_CHECKPOINT_KEY, None)

    @staticmethod
    def _checkpoint_message_key(message: dict[str, Any]) -> tuple[Any, ...]:
        return (
            message.get("role"),
            message.get("content"),
            message.get("tool_call_id"),
            message.get("name"),
            message.get("tool_calls"),
            message.get("reasoning_content"),
            message.get("thinking_blocks"),
        )

    def _restore_runtime_checkpoint(self, session: Session) -> bool:
        """Materialize an unfinished turn into session history before a new request."""
        checkpoint = session.metadata.get(self._RUNTIME_CHECKPOINT_KEY)
        if not isinstance(checkpoint, dict):
            return False

        assistant_message = checkpoint.get("assistant_message")
        completed_tool_results = checkpoint.get("completed_tool_results") or []
        pending_tool_calls = checkpoint.get("pending_tool_calls") or []

        restored_messages: list[dict[str, Any]] = []
        if isinstance(assistant_message, dict):
            restored = dict(assistant_message)
            restored.setdefault("timestamp", datetime.now().isoformat())
            restored_messages.append(restored)
        for message in completed_tool_results:
            if isinstance(message, dict):
                restored = dict(message)
                restored.setdefault("timestamp", datetime.now().isoformat())
                restored_messages.append(restored)
        for tool_call in pending_tool_calls:
            if not isinstance(tool_call, dict):
                continue
            tool_id = tool_call.get("id")
            name = ((tool_call.get("function") or {}).get("name")) or "tool"
            restored_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_id,
                    "name": name,
                    "content": "Error: Task interrupted before this tool finished.",
                    "timestamp": datetime.now().isoformat(),
                }
            )

        overlap = 0
        max_overlap = min(len(session.messages), len(restored_messages))
        for size in range(max_overlap, 0, -1):
            existing = session.messages[-size:]
            restored = restored_messages[:size]
            if all(
                self._checkpoint_message_key(left) == self._checkpoint_message_key(right)
                for left, right in zip(existing, restored)
            ):
                overlap = size
                break
        session.messages.extend(restored_messages[overlap:])

        self._clear_pending_user_turn(session)
        self._clear_runtime_checkpoint(session)
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
