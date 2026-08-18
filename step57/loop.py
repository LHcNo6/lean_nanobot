from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import time
from contextlib import AbstractContextManager, ExitStack, nullcontext, suppress
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import Any, Awaitable, Callable

from step57.bus import MessageBus
from step57.bus.events import InboundMessage, OutboundMessage, StreamDeltaEvent
from step57.bus.outbound_events import (
    RetryWaitEvent,
    StreamDeltaEvent as _StreamDeltaTyped,
    StreamEndEvent,
    StreamedResponseEvent,
    outbound_message_for_event,
)
from step57.bus.progress import build_bus_progress_callback
from step57.bus.runtime_events import (
    RuntimeEventBus,
    RuntimeEventPublisher,
)
from step57.autocompact import AutoCompact
from step57.command import CommandContext, CommandRouter, register_builtin_commands
from step57.consolidation import Consolidator
from step57.context import ContextBuilder, RequestContext
from step57.goal_state import (
    goal_state_runtime_lines,
    runner_wall_llm_timeout_s,
    sustained_goal_active,
)
from step57.hook import (
    AgentHook,
    AgentHookContext,
    AgentTurnHookFactory,
    AgentTurnHookSpec,
    build_agent_turn_hook,
)
from step57.llm import LLMResponse, LLMRuntime, ToolCallRequest
from step57.model_runtime import ModelRuntimeResolver
from step57.memory import MemoryStore
from step57.pairing import PairingStore
from step57.runtime_context import (
    RUNTIME_CONTEXT_HISTORY_META,
    RUNTIME_CONTEXT_MESSAGE_META,
    RuntimeContextBlock,
    append_runtime_context,
    resolve_runtime_context,
)
from step57.runner import (
    AgentRunResult,
    AgentRunSpec,
    AgentRunner,
    _MAX_INJECTIONS_PER_TURN,
)
from step57.security.workspace_access import WorkspaceScopeResolver
from step57.session import Session, SessionManager
from step57.session import turn_continuation
from step57.session.history_visibility import HIDDEN_HISTORY_META
from step57.session.keys import UNIFIED_SESSION_KEY
from step57.subagent import SubagentManager
from step57.tool import ToolRegistry
from step57.context import ToolContext
from step57.loader import ToolLoader
from step57.helpers import image_placeholder_text, truncate_text
from step57.utils.cancellation import task_is_cancelling
from step57.utils.document import reference_non_image_attachments


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
class StateTraceEntry:
    """单个状态执行的追踪记录（step57 新增，对齐 nanobot）。

    用于 loop 可观测性：记录每个状态（restore/build/run/save/respond）的
    执行时长、返回 event 和异常信息，便于调试和性能分析。
    """
    state: TurnState
    started_at: float           # time.perf_counter() 值
    duration_ms: float          # 执行时长（毫秒）
    event: str                  # 状态返回的 event
    error: str | None = None    # 异常时为 "exception"，正常为 None


_REPLAY_SAFETY_BUFFER = 128  # 从 context window 反推 replay budget 时的安全余量

logger = logging.getLogger(__name__)


@dataclass
class TurnContext:
    """一次 turn 的状态载体。

    step28 起新增 request_context / runtime_context_blocks；
    step29 起新增续跑相关字段：original_user_text / visible_run_started_at /
    suppress_response / save_skip / user_persisted_early / all_messages /
    stop_reason / final_content。
    """

    msg: InboundMessage
    session_key: str
    state: TurnState = TurnState.RESTORE
    session: Session | None = None
    # step57：summary 改名为 pending_summary（对齐 nanobot）
    pending_summary: str | None = None
    history: list[dict[str, Any]] = field(default_factory=list)
    initial_messages: list[dict[str, Any]] = field(default_factory=list)
    outbound: OutboundMessage | None = None
    turn_id: str | None = None
    runtime: LLMRuntime | None = None
    request_context: RequestContext | None = None
    runtime_context_blocks: list[RuntimeContextBlock] = field(default_factory=list)
    on_progress: Any | None = None
    on_stream: Any | None = None
    on_stream_end: Any | None = None
    on_retry_wait: Any | None = None
    pending_queue: asyncio.Queue[Any] | None = None
    turn_wall_started_at: float = 0.0
    # step29 新增：续跑 / 持久化语义字段。
    original_user_text: str | None = None
    visible_run_started_at: float | None = None
    suppress_response: bool = False
    save_skip: int = 0
    user_persisted_early: bool = False
    all_messages: list[dict[str, Any]] = field(default_factory=list)
    final_content: str | None = None
    tools_used: list[str] = field(default_factory=list)
    stop_reason: str = ""
    # step57：移除 error 字段（错误通过 final_content + stop_reason 传递）
    had_injections: bool = False
    # step57：usage 字典（tokens 信息）
    usage: dict[str, int] = field(default_factory=dict)
    # step41：ephemeral 临时 turn 模式——不做长期记忆维护（consolidation /
    # enforce_file_cap / 后台压缩），hook 链仅保留 progress hook。
    # run_extra_hooks_for_ephemeral=True 时即使 ephemeral 也执行完整 hook 链。
    ephemeral: bool = False
    run_extra_hooks_for_ephemeral: bool = False
    # step42：turn 延迟（毫秒），_state_save 计算并存入，供 _assemble_outbound 使用
    turn_latency_ms: int | None = None
    # step57：状态执行追踪列表，每个状态执行后 append 一条 StateTraceEntry
    trace: list[StateTraceEntry] = field(default_factory=list)


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
        hook_factories: list[AgentTurnHookFactory] | None = None,
        session_ttl_minutes: int = 0,
        pairing: PairingStore | None = None,
        max_tool_result_chars: int = 16_000,
        config: Any | None = None,
        restrict_to_workspace: bool = False,
        unified_session: bool = False,
        max_iterations: int = 5,
    ) -> None:
        """初始化 AgentLoop。

        Args:
            bus: 消息总线。
            provider: LLM provider。
            registry: 工具注册表。
            session_manager: 会话管理器。
            context_builder: 上下文构建器。
            memory: 记忆库。
            identity: 人格文本。
            replay_budget: replay 预算（与 runtime 二选一）。
            runtime: LLMRuntime（与 replay_budget 二选一）。
            subagent_manager: 子代理管理器。
            hooks: 静态 hook 列表。
            session_ttl_minutes: 空闲压缩阈值。
            pairing: 配对存储。
            max_tool_result_chars: 工具结果截断上限。
            config: 装配时的 Config（供工具上下文解析配置）。
            restrict_to_workspace: 权限意图 —— 默认限制工具文件访问在
                workspace 内（对齐 nanobot ``config.tools.restrict_to_workspace``）。
            unified_session: True 时所有通道共享一个会话
                （``unified:default``，对齐 nanobot ``agents.defaults.unified_session``）。
            max_iterations: 单 turn 最大工具迭代次数（对齐 nanobot
                ``AgentDefaults.max_tool_iterations``，默认 5 保持学习版轻量）。
        """
        self.bus = bus
        self.provider = provider
        self.registry = registry
        self.sessions = session_manager
        self.context = context_builder
        self.memory = memory
        self.identity = identity
        self.max_tool_result_chars = max_tool_result_chars
        self.max_iterations = max_iterations  # step36: 替代 _build_agent_spec 硬编码 5
        self.config = config  # step27: 装配时的 Config（供工具上下文解析配置）
        self.subagents = subagent_manager
        self.hooks = list(hooks) if hooks else []
        self._hook_factories = list(hook_factories) if hook_factories else []
        self.pairing = pairing
        self.commands = CommandRouter()
        register_builtin_commands(self.commands)
        self.running = False
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._pending_queues: dict[str, asyncio.Queue[InboundMessage]] = {}
        # step57：跟踪后台任务，shutdown 时 drain
        self._background_tasks: list[asyncio.Task] = []
        # step29：session_key -> 进行中任务的跟踪表（/stop 与并发门控用）。
        self._active_tasks: dict[str, list[asyncio.Task]] = {}
        # step29：全局并发门控（跨会话并发上限；<=0 表示不限）。
        raw_max = os.environ.get("NANOBOT_MAX_CONCURRENT_REQUESTS", "3")
        try:
            _max = int(raw_max)
        except (TypeError, ValueError):
            _max = 3
        self._concurrency_gate: asyncio.Semaphore | None = (
            asyncio.Semaphore(_max) if _max > 0 else None
        )
        self.unified_session = unified_session
        self._runner = AgentRunner()
        # step29: workspace 权限范围解析器（默认根 = context_builder.workspace）。
        self.restrict_to_workspace = restrict_to_workspace
        self.workspace_scopes = WorkspaceScopeResolver(
            default_workspace=context_builder.workspace,
            default_restrict_to_workspace=restrict_to_workspace,
        )
        # step29: 每 turn 前解析一次的运行时上下文提供器（loop 级注册）。
        self._runtime_context_providers: list[Any] = []
        # step27: 进程内运行时事件总线（turn 生命周期观测）。外部订阅者可
        # 用 ``loop.runtime_events.subscribe(...)`` 注册 handler。
        self.runtime_events = RuntimeEventBus()
        self.runtime_event_publisher = RuntimeEventPublisher(self.runtime_events)
        if runtime is not None:
            initial_runtime = runtime
        elif replay_budget is not None:
            initial_runtime = LLMRuntime.capture(
                provider=provider,
                model=getattr(provider, "model", None),
                context_window_tokens=max(replay_budget, 0),
                max_tokens=4096,
            )
        else:
            raise ValueError("AgentLoop requires replay_budget or runtime")
        # step57：runtime 管理委托给 ModelRuntimeResolver
        self._runtime_resolver = ModelRuntimeResolver(initial_runtime)
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
        from step57.providers.factory import make_provider

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
            or ContextBuilder(
                workspace=workspace,
                disabled_skills=list(defaults.disabled_skills),
            ),
            memory=extra.pop("memory", None) or MemoryStore(workspace=workspace),
            identity=extra.pop("identity", None)
            or f"You are {defaults.bot_name}, a lightweight AI agent.",
            runtime=runtime,
            session_ttl_minutes=extra.pop("session_ttl_minutes", defaults.session_ttl_minutes),
            max_tool_result_chars=extra.pop("max_tool_result_chars", defaults.max_tool_result_chars),
            # step38：配置层接入 max_tool_iterations（默认 200），
            # 替代 __init__ 硬编码默认 5；extra 可覆盖（测试用小值）。
            max_iterations=extra.pop("max_iterations", defaults.max_tool_iterations),
            config=config,
            # step29: 权限意图贯通 —— config.tools.restrict_to_workspace 驱动
            # WorkspaceScopeResolver 与工具装配。
            restrict_to_workspace=extra.pop(
                "restrict_to_workspace",
                getattr(config.tools, "restrict_to_workspace", False),
            ),
            unified_session=extra.pop(
                "unified_session",
                getattr(defaults, "unified_session", False),
            ),
            **extra,
        )

    def _schedule_background(self, coro: Any) -> None:
        """调度后台协程为被跟踪的 asyncio.Task（step57：登记+完成自动移除）。

        Args:
            coro: 要调度的协程对象。
        """
        task = asyncio.create_task(coro)
        self._background_tasks.append(task)
        task.add_done_callback(self._background_tasks.remove)

    async def run(self) -> None:
        """主消费循环：持续接收入站消息并分派为任务。

        step29（A13）：消费循环对集成层泄漏的 CancelledError 免疫
        （``task_is_cancelling()`` 为真时才是真正被取消，否则记日志并继续）；
        每个分派任务登记进 ``_active_tasks``（按 session_key），供 /stop
        取消；priority 命令（如 /stop）绕过会话锁内联执行。
        """
        self.running = True
        while self.running:
            try:
                msg = await asyncio.wait_for(self.bus.consume_inbound(), timeout=1.0)
            except asyncio.TimeoutError:
                self.auto_compact.check_expired(
                    self._schedule_background,
                    lambda: self.runtime,
                    active_session_keys=set(self._pending_queues),
                )
                continue
            except asyncio.CancelledError:
                # 真正的任务取消要传播（让 shutdown 干净退出）；仅当不是
                # 当前任务的取消时，把泄漏的 CancelledError 当噪音吞掉。
                if not self.running or task_is_cancelling():
                    raise
                logger.warning(
                    "Ignoring leaked CancelledError while consuming inbound messages"
                )
                continue
            except Exception as exc:
                logger.warning("Error consuming inbound message: %s, continuing...", exc)
                continue
            self.auto_compact.check_expired(
                self._schedule_background,
                lambda: self.runtime,
                active_session_keys=set(self._pending_queues),
            )
            # step29：priority 命令（/stop 等）需要绕过会话锁在消费循环内联
            # 执行，否则无法打断正在进行的 turn。
            if self.commands.is_priority(msg.content.strip()):
                await self._dispatch_command_inline(
                    msg, self._effective_session_key(msg), msg.content.strip(),
                    self.commands.dispatch_priority,
                )
                continue
            key = self._effective_session_key(msg)
            # step29（A14 && 回归修复）：若该会话已有未完成的分派任务，则把
            # 本消息直接放入该会话的 pending 队列，由正在进行的 turn 在
            # checkpoint 时注入；否则再创建新任务。这样可以确定性保证
            # "turn 进行中到达的消息走注入路径"，避免与 `lock.locked()`
            # 检查竞态：任务可能等到上一 turn 已结束后才启动，从而被当成
            # 一次独立的新 turn（导致注入测试断言失败）。
            if any(not t.done() for t in self._active_tasks.get(key, ())):
                await self._get_or_create_queue(key).put(msg)
                continue
            task = asyncio.create_task(self._dispatch(msg))
            self._active_tasks.setdefault(key, []).append(task)
            task.add_done_callback(
                lambda t, k=key: (
                    self._active_tasks.get(k, []).remove(t)
                    if t in self._active_tasks.get(k, [])
                    else None
                )
            )

    async def _dispatch_command_inline(
        self,
        msg: InboundMessage,
        key: str,
        raw: str,
        dispatch_fn: Callable[[CommandContext], Awaitable[OutboundMessage | None]],
    ) -> None:
        """在 run() 循环中内联分派命令并发布结果（不占用会话锁）。

        Args:
            msg: 触发命令的入站消息。
            key: 有效会话键。
            raw: 原始命令文本。
            dispatch_fn: 命令路由的派发函数（priority 档）。
        """
        ctx = CommandContext(msg=msg, session=None, key=key, raw=raw, loop=self)
        result = await dispatch_fn(ctx)
        if result:
            await self.bus.publish_outbound(result)
        else:
            logger.warning("Command '%s' matched but dispatch returned None", raw)

    async def _cancel_active_tasks(self, key: str) -> int:
        """取消并等待 *key* 的所有活动任务（含子代理），返回取消数量。

        Args:
            key: session_key。

        Returns:
            取消的任务总数（含子代理任务）。
        """
        tasks = self._active_tasks.pop(key, [])
        cancelled = sum(1 for t in tasks if not t.done() and t.cancel())
        for t in tasks:
            with suppress(asyncio.CancelledError, Exception):
                await t
        sub_cancelled = 0
        if self.subagents is not None:
            sub_cancelled = await self.subagents.cancel_by_session(key)
        return cancelled + sub_cancelled

    def stop(self) -> None:
        self.running = False

    # step57：runtime 管理委托给 ModelRuntimeResolver

    @property
    def runtime(self) -> LLMRuntime:
        """当前不可变运行时设置（委托给 ModelRuntimeResolver）。"""
        return self._runtime_resolver.runtime

    @property
    def llm_runtime(self) -> LLMRuntime:
        """解析下一个 turn 使用的运行时（step57：refresh 为 no-op）。"""
        return self._runtime_resolver.current(refresh=True)

    @property
    def model_preset(self) -> str | None:
        """当前选中的模型预设名。"""
        return self._runtime_resolver.model_preset

    @property
    def provider_signature(self) -> tuple[object, ...] | None:
        """当前 provider 快照签名。"""
        return self._runtime_resolver.provider_signature

    def set_model_preset(self, name: str | None) -> LLMRuntime:
        """选择命名预设为未来 turn 的默认运行时。

        Args:
            name: 预设名，None 表示清除预设。

        Returns:
            切换后的 LLMRuntime。
        """
        return self._runtime_resolver.select_preset(name)

    def set_runtime_model(self, model: str) -> LLMRuntime:
        """在当前 provider 上切换模型。

        Args:
            model: 新模型名。

        Returns:
            切换后的 LLMRuntime。
        """
        return self._runtime_resolver.select_model(model)

    def set_runtime_context_window(self, context_window_tokens: int) -> LLMRuntime:
        """切换上下文窗口大小。

        Args:
            context_window_tokens: 新的上下文窗口 token 数。

        Returns:
            切换后的 LLMRuntime。
        """
        return self._runtime_resolver.select_context_window(context_window_tokens)

    async def close_mcp(self) -> None:
        """等待所有后台任务完成（step57：background tasks drain）。

        nanobot 中此方法还关闭 MCP 连接；learn_nano 暂无 MCP，
        仅 drain 后台任务（如 consolidate_by_tokens）。
        """
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
            self._background_tasks.clear()

    def _runtime_events(self) -> RuntimeEventPublisher:
        """返回本 loop 的运行时事件发布器（进程内 pub/sub）。"""

        return self.runtime_event_publisher

    def register_runtime_context_provider(self, provider: Any) -> None:
        """注册一个每 turn 前解析一次的运行时上下文提供器（对齐 nanobot）。

        提供器签名：``async (RequestContext) -> RuntimeContextBlock | list | None``。
        与工具自带提供器（``Tool.runtime_context_provider``）合并后统一解析。
        """
        if provider not in self._runtime_context_providers:
            self._runtime_context_providers.append(provider)

    def _build_turn_request_context(
        self,
        msg: InboundMessage,
        session: Session | None,
        session_key: str,
        *,
        runtime: LLMRuntime | None = None,
        turn_id: str | None = None,
    ) -> RequestContext:
        """按消息 + 会话构建富 RequestContext（workspace 取 scope 解析值）。

        供 ``_state_build`` 与 ``_process_system_message`` 两条路径复用
        （对齐 nanobot ``loop._request_context_for_turn``）。
        """
        scope = self.workspace_scopes.for_message(
            msg, session.metadata if session is not None else None
        )
        metadata = msg.metadata if isinstance(msg.metadata, dict) else {}
        return RequestContext(
            channel=msg.channel,
            chat_id=msg.chat_id,
            message_id=metadata.get("message_id"),
            session_key=session_key,
            original_user_text=msg.content if isinstance(msg.content, str) else None,
            runtime=runtime,
            metadata=dict(metadata),
            sender_id=msg.sender_id,
            turn_id=turn_id,
            workspace=scope.project_path,
        )

    async def _resolve_runtime_context_for_turn(
        self,
        ctx: TurnContext,
    ) -> list[RuntimeContextBlock]:
        """解析本 turn 的运行时上下文块：工具提供器 + loop 注册提供器。

        顺序：先工具提供器（按注册表排序），后 loop 级提供器（注册顺序），
        全部串行解析（对齐 nanobot ``loop._resolve_runtime_context_for_turn``）。
        """
        providers = [
            *self.registry.get_runtime_context_providers(),
            *self._runtime_context_providers,
        ]
        assert ctx.request_context is not None
        return await resolve_runtime_context(providers, ctx.request_context)

    async def _build_bus_progress_callback(
        self, msg: InboundMessage
    ) -> Callable[..., Awaitable[None]]:
        """构建发布到消息总线的进度回调（ProgressEvent）。

        对齐 nanobot ``loop._build_bus_progress_callback``：交由
        ``bus/progress.build_bus_progress_callback`` 组装，runner 的工具进度
        最终以 ProgressEvent 形式出现在 outbound 队列上。
        """
        return build_bus_progress_callback(self.bus, msg)

    async def _build_retry_wait_callback(
        self, msg: InboundMessage
    ) -> Callable[[str], Awaitable[None]]:
        """构建发布到消息总线的重试等待回调（RetryWaitEvent）。

        对齐 nanobot ``loop._build_retry_wait_callback``：provider 重试心跳
        文本会以 RetryWaitEvent 出现在 outbound 队列上，供通道/UI 渲染。
        """

        async def _on_retry_wait(content: str) -> None:
            await self.bus.publish_outbound(
                outbound_message_for_event(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    event=RetryWaitEvent(content=content),
                    metadata=msg.metadata,
                )
            )

        return _on_retry_wait

    def _get_or_create_queue(self, session_key: str) -> asyncio.Queue[InboundMessage]:
        if session_key not in self._pending_queues:
            self._pending_queues[session_key] = asyncio.Queue(maxsize=20)
        return self._pending_queues[session_key]

    def _effective_session_key(self, msg: InboundMessage) -> str:
        """返回用于任务路由 / 会话隔离的有效会话键（H8）。

        统一会话开启且无显式覆盖时，所有消息路由到 ``unified:default``；
        否则使用消息声明的 session_key（通道消息的会话键形如
        ``channel:chat_id``）。
        """
        if self.unified_session and not msg.session_key_override:
            return UNIFIED_SESSION_KEY
        return msg.session_key_override or msg.session_key or msg.chat_id

    async def _dispatch(self, msg: InboundMessage) -> None:
        """分派一条入站消息：会话内串行、跨会话受并发门控。

        step29（A13）：``_concurrency_gate`` 限制全局并发 turn 数；
        会话锁忙时消息进 pending queue 做 mid-turn 注入；CancelledError
        （/stop）时先物化 checkpoint 再传播；续跑 pending 时不发
        turn_completed/run_status 事件（后续片接管）；队列遗留消息
        re-publish 回总线（隐形续跑依赖此机制回流）。
        """
        session_key = self._effective_session_key(msg)
        if session_key != msg.session_key:
            msg = dataclasses.replace(msg, session_key_override=session_key)
        lock = self._session_locks.setdefault(session_key, asyncio.Lock())
        gate = self._concurrency_gate or nullcontext()
        if lock.locked():
            # Session busy: route the message into its pending queue for
            # mid-turn injection instead of creating a competing task.
            await self._get_or_create_queue(session_key).put(msg)
            return
        pending: asyncio.Queue[InboundMessage] | None = None
        try:
            async with lock, gate:
                pending = self._get_or_create_queue(session_key)
                response = await self._process_message(
                    msg, session_key, pending_queue=pending, runtime=self.runtime,
                )
                if response is not None:
                    await self.bus.publish_outbound(response)
                elif msg.channel == "cli" and not (
                    turn_continuation.internal_continuation_pending(msg.metadata)
                ):
                    # CLI 无响应时也要回一条空消息保持输入提示活性；续跑
                    # pending 时跳过（最终响应由续跑片产出）。
                    await self.bus.publish_outbound(OutboundMessage(
                        channel=msg.channel, chat_id=msg.chat_id,
                        content="", metadata=msg.metadata or {},
                    ))
        except asyncio.CancelledError:
            # /stop 场景：物化 checkpoint（保留工具进度）后传播取消。
            try:
                key = self._effective_session_key(msg)
                session = self.sessions.get_or_create(key)
                if self._restore_runtime_checkpoint(session):
                    self._clear_pending_user_turn(session)
                    self.sessions.save(session)
                    logger.info(
                        "Restored partial context for cancelled session %s", key
                    )
            except Exception:
                logger.debug(
                    "Could not restore checkpoint for cancelled session %s",
                    session_key,
                    exc_info=True,
                )
            raise
        except Exception as exc:
            logging.getLogger(__name__).exception(
                "Error processing message for session %s", session_key
            )
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content="Sorry, I encountered an error.",
                    metadata=msg.metadata or {},
                )
            )
        finally:
            continuing = turn_continuation.internal_continuation_pending(msg.metadata)
            if not continuing:
                # step27: turn 生命周期事件——无论成败都标记完成并将 run status
                # 复位为 idle；续跑 pending 时由续跑片接管，不发事件。
                await self._runtime_events().turn_completed(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    session_key=session_key,
                    metadata=msg.metadata,
                )
                await self._runtime_events().run_status_changed(
                    msg, session_key, "idle"
                )
                self._runtime_events().clear_turn(session_key)
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
        ephemeral: bool = False,
        run_extra_hooks_for_ephemeral: bool = False,
    ) -> OutboundMessage | None:
        if msg.channel == "system":
            return await self._process_system_message(
                msg, runtime=runtime, pending_queue=pending_queue,
            )

        # step29：续跑入站消息不当作用户输入（original_user_text=None、
        # 不做 user 持久化），可见运行起点从 metadata 恢复以便 latency 全程。
        original_user_text = (
            None
            if turn_continuation.internal_continuation_inbound(msg.metadata)
            else msg.content
        )
        ctx = TurnContext(
            msg=msg,
            session_key=session_key,
            turn_id=f"{session_key}:{time.time_ns()}",
            runtime=runtime or self.runtime,
            pending_queue=pending_queue,
            turn_wall_started_at=time.time(),
            original_user_text=original_user_text,
            visible_run_started_at=turn_continuation.internal_continuation_run_started_at(
                msg.metadata,
            ),
            ephemeral=ephemeral,
            run_extra_hooks_for_ephemeral=run_extra_hooks_for_ephemeral,
        )
        while ctx.state != TurnState.DONE:
            handler_name = f"_state_{ctx.state.name.lower()}"
            handler = getattr(self, handler_name)

            # step57：记录状态开始时间（perf_counter 高精度计时）
            t0 = time.perf_counter()
            try:
                event = await handler(ctx)
            except Exception as exc:
                # step57：异常时记录 trace（error="exception"），保持现有 break 逻辑
                duration = (time.perf_counter() - t0) * 1000
                ctx.trace.append(StateTraceEntry(
                    state=ctx.state,
                    started_at=t0,
                    duration_ms=duration,
                    event="",
                    error="exception",
                ))
                ctx.outbound = OutboundMessage(
                    content=f"Error: {exc}", metadata={"stop_reason": "error"},
                )
                break

            # step57：正常完成时记录 trace
            duration = (time.perf_counter() - t0) * 1000
            ctx.trace.append(StateTraceEntry(
                state=ctx.state,
                started_at=t0,
                duration_ms=duration,
                event=event,
            ))

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
        # step27: turn 生命周期事件——会话 turn 已加载 session，协议本文将构建上下文。
        await self._runtime_events().session_turn_started(ctx.msg, ctx.session_key)
        if self._restore_runtime_checkpoint(ctx.session):
            self.sessions.save(ctx.session)
        if self._restore_pending_user_turn(ctx.session):
            self.sessions.save(ctx.session)
        return "ok"

    @staticmethod
    def _replay_token_budget(runtime: Any) -> int:
        """从 context window 推导 session 历史回放的 token 预算。

        对齐 nanobot ``AgentLoop._replay_token_budget``。

        公式：``context_window_tokens - max(1, max_output) - 1024``，
        若结果 <= 0 则返回 ``max(128, context_window_tokens // 2)``。

        Args:
            runtime: LLMRuntime 对象。

        Returns:
            回放 token 预算。
        """
        if runtime.context_window_tokens <= 0:
            return 0
        max_output = getattr(runtime, "max_tokens", None) or getattr(runtime, "generation", None)
        if max_output is None:
            reserved_output = 4096
        elif hasattr(max_output, "max_tokens"):
            try:
                reserved_output = int(max_output.max_tokens)
            except (TypeError, ValueError):
                reserved_output = 4096
        else:
            try:
                reserved_output = int(max_output)
            except (TypeError, ValueError):
                reserved_output = 4096
        budget = runtime.context_window_tokens - max(1, reserved_output) - 1024
        return budget if budget > 0 else max(128, runtime.context_window_tokens // 2)

    async def _state_compact(self, ctx: TurnContext) -> str:
        ctx.session, pending = self.auto_compact.prepare_session(
            ctx.session, ctx.session_key
        )
        # step57：summary → pending_summary
        ctx.pending_summary = pending
        if ctx.pending_summary is None:
            meta = ctx.session.metadata.get("_last_summary")
            ctx.pending_summary = meta.get("text") if isinstance(meta, dict) else None
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

        # step57：shortcut 命令持久化 user+assistant（_command 标记）
        # /new 排除（它会清空会话）
        if raw.lower() != "/new":
            ctx.user_persisted_early = self._persist_user_message_early(
                ctx.msg, ctx.session, _command=True,
            )
            ctx.session.add_message(
                "assistant", result.content, _command=True,
            )
            self.sessions.save(ctx.session)
            self._clear_pending_user_turn(ctx.session)

        return "shortcut"

    def _prepare_message_media(
        self, content: str, media: list[str],
    ) -> tuple[str, list[str]]:
        """处理消息附件：非图片追加引用，图片路径保留（step57）。

        step57 简化版：总是调用 reference_non_image_attachments（不提取
        文档文本，因为无 channels_config 和文档解析依赖）。

        Args:
            content: 原始消息文本。
            media: 附件路径列表。

        Returns:
            (更新后的 content, 图片路径列表)
        """
        return reference_non_image_attachments(content, media)

    def _build_initial_messages(
        self,
        msg: InboundMessage,
        session: Session,
        history: list[dict[str, Any]],
        pending_summary: str | None,
        runtime_context_blocks: list[RuntimeContextBlock] | None = None,
        current_role: str = "user",
        include_memory_recent_history: bool = True,
    ) -> list[dict[str, Any]]:
        """构建 LLM turn 的初始消息列表。

        step34：从 ``_state_build`` / ``_process_system_message`` 中提取，
        统一 initial_messages 构建逻辑，避免重复代码。

        对齐 nanobot ``_build_initial_messages``（loop.py:663），但暂不包含
        media / channel / chat_id / sender_id / session_metadata /
        session_key / unified_session 等参数（需要媒体处理和统一会话基础设施，
        留待后续 step）。

        step41：新增 ``include_memory_recent_history`` 参数（对齐 nanobot），
        ephemeral turn 传 False——临时 turn 不读取跨会话记忆，避免 dream 场景
        的记忆循环。step41 中 context.py 尚无 memory 集成，该参数为接口对齐
        （no-op），等 memory 集成后填充实际逻辑。

        Args:
            msg: 入站消息。
            session: 当前会话。
            history: 会话历史消息（已通过 ``get_history`` 获取）。
            pending_summary: 会话摘要（pending consolidation summary）。
            runtime_context_blocks: 运行时上下文块；仅 ``current_role=="user"``
                时追加到用户内容尾部。
            current_role: 尾部消息角色（"user" / "assistant"）。
                subagent follow-up 时为 "assistant"，current_message 为空。
            include_memory_recent_history: 是否在 system prompt 中包含跨会话
                记忆（ephemeral turn 为 False）。

        Returns:
            ``[system, *history, tail]`` 消息列表。
        """
        scope = self.workspace_scopes.for_message(msg, session.metadata)
        goal_lines = goal_state_runtime_lines(session.metadata)
        identity = self.identity
        if goal_lines:
            identity = identity + "\n\n" + "\n".join(goal_lines)
        current_message = msg.content if current_role == "user" else ""
        # step57：处理 media 附件（非图片追加引用，图片追加占位符）
        if current_role == "user" and msg.media:
            current_message, image_paths = self._prepare_message_media(
                current_message, msg.media,
            )
            if image_paths:
                placeholders = "\n".join(
                    image_placeholder_text(p) for p in image_paths
                )
                current_message = (
                    f"{current_message}\n{placeholders}"
                    if current_message else placeholders
                )
        return self.context.build_messages(
            current_message=current_message,
            history=history,
            identity=identity,
            session_summary=pending_summary,
            current_role=current_role,
            runtime_context_blocks=runtime_context_blocks,
            workspace=scope.project_path,
            include_memory_recent_history=include_memory_recent_history,
        )

    def _persist_user_message_early(
        self,
        msg: InboundMessage,
        session: Session,
        runtime_context_blocks: list[RuntimeContextBlock] | None = None,
        **kwargs: Any,
    ) -> bool:
        """在 turn 开始前持久化触发用户消息。

        step34：对齐 nanobot ``_persist_user_message_early``（loop.py:628）。
        核心差异是持久化的用户消息**包含运行时上下文 + marker**（而 step33
        只持久化原始文本），这样下一轮历史回放时 LLM 能看到上一轮的运行时
        上下文（如时间、工作目录等）。

        暂不包含：media 处理、``agent_context.session_extra``（cli_app/mcp）、
        ``automation_history_overrides``（cron/local trigger 文本覆盖）。
        这些需要对应基础设施，留待后续 step。``**kwargs`` 预留扩展点。

        Args:
            msg: 入站消息。
            session: 当前会话。
            runtime_context_blocks: 运行时上下文块，会附加到用户文本尾部
                并持久化 marker。
            **kwargs: 额外的消息元数据（如 media、mcp_presets 等，预留）。

        Returns:
            True 如果消息被持久化，False 否则。
        """
        if not turn_continuation.should_persist_user_message(msg.metadata):
            return False
        has_text = isinstance(msg.content, str) and msg.content.strip()
        if not (has_text or runtime_context_blocks):
            return False

        extra: dict[str, Any] = dict(kwargs)
        text = msg.content if isinstance(msg.content, str) else ""

        # 附加运行时上下文 + marker（对齐 nanobot：持久化的用户消息含运行时上下文）
        text, runtime_context_meta = append_runtime_context(
            text,
            runtime_context_blocks or (),
        )
        if runtime_context_meta is not None:
            extra[RUNTIME_CONTEXT_HISTORY_META] = runtime_context_meta

        session.add_message("user", text, **extra)
        self._mark_pending_user_turn(session)
        self.sessions.save(session)
        return True

    async def _state_build(self, ctx: TurnContext) -> str:
        # step33：根据 context window 计算 replay_max_messages，在 _state_build
        # 中调用 consolidation（对齐 nanobot），而不是在 _state_compact 中。
        from step57.session.manager import replay_max_messages_for_context

        runtime = ctx.runtime or self.runtime
        replay_max_messages = replay_max_messages_for_context(runtime.context_window_tokens)
        # step41：ephemeral turn 跳过 build 阶段 consolidation（临时 turn 不需要
        # token 预算压缩，避免额外 LLM 调用）。
        if not ctx.ephemeral:
            await self.consolidator.maybe_consolidate_by_tokens(
                ctx.session,
                runtime=runtime,
                replay_max_messages=replay_max_messages,
            )
        # step42：MessageTool 每个 turn 开始时重置 _sent_in_turn 标记
        from step57.tools.message import MessageTool
        if message_tool := self.registry.get("message"):
            if isinstance(message_tool, MessageTool):
                message_tool.start_turn()
        ctx.history = ctx.session.get_history(
            max_messages=replay_max_messages,
            max_tokens=self._replay_token_budget(runtime),
            extend_to_user=False,
        )
        # step29: 解析本 turn 的运行时上下文（工具/loop provider）并随
        # initial_messages 附加到当前用户消息。
        ctx.request_context = self._build_turn_request_context(
            ctx.msg, ctx.session, ctx.session_key,
            runtime=ctx.runtime, turn_id=ctx.turn_id,
        )
        ctx.runtime_context_blocks = await self._resolve_runtime_context_for_turn(ctx)
        # step34：先构建 initial_messages，再持久化用户消息（对齐 nanobot 顺序）。
        # _build_initial_messages 内部计算 goal_lines / identity / scope。
        ctx.initial_messages = self._build_initial_messages(
            ctx.msg, ctx.session, ctx.history, ctx.pending_summary,
            runtime_context_blocks=ctx.runtime_context_blocks,
            include_memory_recent_history=not ctx.ephemeral,
        )
        # step34：持久化含运行时上下文 + marker 的用户消息（对齐 nanobot）。
        # 续跑消息/_skip_user_persist 不持久化（由 should_persist_user_message 检查）。
        ctx.user_persisted_early = self._persist_user_message_early(
            ctx.msg, ctx.session,
            runtime_context_blocks=ctx.runtime_context_blocks,
        )
        return "ok"

    def _sync_subagent_runtime_limits(self) -> None:
        """将 loop 级运行时限制同步到 subagent 管理器（step36，对齐 nanobot）。

        目前仅同步 ``max_iterations``：subagent 由外部传入构造，其默认
        ``max_iterations=10`` 可能与 loop 的 ``self.max_iterations`` 不一致，
        每次 turn 运行前同步一次，确保 spawn 出的 subagent 使用相同的迭代上限。

        ``self.subagents`` 为 None 时（测试中常不传入）直接返回，不报错。
        """
        if self.subagents is None:
            return
        self.subagents.max_iterations = self.max_iterations

    def _build_agent_spec(
        self,
        msg: InboundMessage,
        session_key: str,
        session: Session | None,
        initial_messages: list[dict[str, Any]],
        *,
        injection_callback: Callable[..., Awaitable[list[dict]]] | None = None,
        checkpoint_callback: Callable[..., Awaitable[None]] | None = None,
        retry_wait_callback: Callable[[str], Awaitable[None]] | None = None,
        progress_callback: Callable[..., Awaitable[None]] | None = None,
        request_context: RequestContext | None = None,
        workspace_scope: Any | None = None,
        pending_budget_available: bool = False,
        hook_factories: list[AgentTurnHookFactory] | None = None,
        ephemeral: bool = False,
        run_extra_hooks_for_ephemeral: bool = False,
    ) -> AgentRunSpec:
        """装配一次 agent 运行的完整参数（step29 起含 workspace 上下文）。

        Args:
            request_context: 本 turn 的富 RequestContext（runner 绑定后工具可查）。
            workspace_scope: 本 turn 的 WorkspaceScope（runner 绑定后工具可查）。
            pending_budget_available: 本 turn 是否持有可排班续跑的 pending
                queue——True 时预算边界策略交给 turn_continuation 判定（可能
                关闭 runner 收尾、走隐形续跑）；False（默认）维持旧行为
                （max_iterations 产出 fallback 文案）。
            hook_factories: turn 级 hook 工厂（仅本 turn 有效）。
            ephemeral: 是否临时 turn（True 时 hook 链仅保留 progress hook）。
            run_extra_hooks_for_ephemeral: 临时 turn 是否也跑额外 hook。
        """
        # step29: 工具拿到真实 workspace（effective scope 的 project_path）与
        # 权限意图，替代 step27 的 ``workspace=""`` / 仅靠 config 的硬编码路径。
        scope = workspace_scope or self.workspace_scopes.for_message(
            msg, session.metadata if session is not None else None
        )

        # step57：闭包动态读取 session.metadata 中的目标状态行
        def _goal_continue() -> str | None:
            """动态构造目标续跑消息，无活跃目标时返回 None。"""
            _goal_lines = goal_state_runtime_lines(
                session.metadata if session is not None else None
            )
            if not _goal_lines:
                return None
            return (
                "You have an active sustained goal:\n\n"
                + "\n".join(_goal_lines)
                + "\n\nPlease continue working toward the objective using your tools, "
                "or call update_goal with action='complete' if the work is truly finished."
            )

        tool_ctx = ToolContext(
            config=self.config,
            workspace=str(scope.project_path),
            restrict_to_workspace=scope.restrict_to_workspace,
            bus=self.bus, subagent_manager=self.subagents,
            sessions=self.sessions, session_key=session_key,
        )
        ToolLoader().load(tool_ctx, self.registry, scope="core")

        # step32：turn 级 hook 装配改用 build_agent_turn_hook（对齐 nanobot
        # ``AgentTurnHookSpec`` 工厂）：progress hook 底座 + 静态 hooks。
        # on_stream 发布 StreamDeltaEvent（与旧 StreamPublishingHook 同语义），
        # on_progress 用 bus 进度回调（tool_hint/tool_events/reasoning 均支持）。
        # step57：_wants_stream 时启用 stream_id 分段（对齐 nanobot）。
        _wants_stream = bool((msg.metadata or {}).get("_wants_stream"))
        stream_base_id = f"{session_key}:{time.time_ns()}" if _wants_stream else None
        stream_segment = 0

        def _current_stream_id() -> str | None:
            """返回当前流式段 ID（step57）。"""
            if stream_base_id is None:
                return None
            return f"{stream_base_id}:{stream_segment}"

        async def _publish_delta(text: str) -> None:
            if not text:
                return
            if _wants_stream:
                # step57：typed event 路径，带 stream_id
                await self.bus.publish_outbound(outbound_message_for_event(
                    channel=msg.channel, chat_id=msg.chat_id,
                    event=_StreamDeltaTyped(content=text, stream_id=_current_stream_id()),
                    metadata=msg.metadata,
                ))
            else:
                await self.bus.publish_outbound(StreamDeltaEvent(
                    content=text, channel=msg.channel, chat_id=msg.chat_id,
                    finished=False, session_key=session_key,
                ))

        async def _publish_stream_end(*, resuming: bool = False, **_: Any) -> None:
            # step32：对齐 nanobot——关流语义走 typed ``StreamEndEvent``：
            # resuming=True 表示流保持存活（工具执行/注入续跑接管），由
            # manager 路由到 send_delta(stream_end=True, resuming=...)。
            # step57：带 stream_id，resuming=False 时递增 segment。
            nonlocal stream_segment
            await self.bus.publish_outbound(outbound_message_for_event(
                channel=msg.channel, chat_id=msg.chat_id,
                event=StreamEndEvent(
                    stream_id=_current_stream_id(),
                    resuming=resuming,
                ),
            ))
            if not resuming:
                stream_segment += 1

        hook = build_agent_turn_hook(AgentTurnHookSpec(
            on_progress=progress_callback,
            on_stream=_publish_delta,
            on_stream_end=_publish_stream_end,
            channel=msg.channel,
            chat_id=msg.chat_id,
            message_id=(msg.metadata or {}).get("message_id"),
            session_key=session_key,
            workspace=str(scope.project_path),
            tool_hint_max_length=40,
            registered_hook_factories=self._hook_factories,
            turn_hook_factories=list(hook_factories or []),
            registered_hooks=list(self.hooks),
            ephemeral=ephemeral,
            run_extra_hooks_for_ephemeral=run_extra_hooks_for_ephemeral,
        ))

        rounds = session.metadata.get("_goal_continuation_rounds", 0) if session else 0
        return AgentRunSpec(
            initial_messages=initial_messages,
            tools=self.registry,
            provider=self.provider,
            max_iterations=self.max_iterations,  # step36: 替代硬编码 5
            # step37：持续目标 turn 禁用 LLM 超时（返回 0.0），普通 turn 用默认。
            llm_timeout_s=runner_wall_llm_timeout_s(
                self.sessions, session_key,
                metadata=session.metadata if session else None,
                message_metadata=msg.metadata,
            ),
            hook=hook,
            session_key=session_key,
            request_context=request_context,
            workspace_scope=scope,
            injection_callback=injection_callback,
            checkpoint_callback=checkpoint_callback,
            # step57：闭包动态读取 session.metadata 中的目标状态
            goal_active_predicate=lambda: (
                sustained_goal_active(session.metadata) if session else False
            ),
            # step57：闭包动态读取 session.metadata（替代静态字符串）
            goal_continue_message=_goal_continue,
            goal_continuation_rounds=rounds,
            retry_wait_callback=retry_wait_callback,
            progress_callback=progress_callback,
            model=self.runtime.model,
            temperature=self.runtime.generation.temperature,
            max_tokens=self.runtime.generation.max_tokens,
            context_window_tokens=self.runtime.context_window_tokens,
            # step29：预算边界是否收尾（runner 合成 fallback 文案）由
            # turn_continuation 策略决定——可续跑时 False，由隐形续跑接管。
            finalize_on_max_iterations=turn_continuation.should_finalize_on_max_iterations(
                pending_queue_available=pending_budget_available,
                session_metadata=session.metadata if session else None,
                message_metadata=msg.metadata,
            ),
        )

    @staticmethod
    def _pending_to_user_message(pending_msg: InboundMessage) -> dict[str, Any]:
        """把 pending 队列里的入站消息规范化为注入用 user 行。

        step29：subagent 结果行打 ``_hidden_history`` 标记（A12）——该行
        保留在 LLM 上下文与持久化历史中，但 /history 展示与 runner 合并
        防护会识别它（不并入上一行 user，保持标记独立）。
        """
        row: dict[str, Any] = {"role": "user", "content": pending_msg.content}
        metadata = pending_msg.metadata if isinstance(pending_msg.metadata, dict) else {}
        if (
            pending_msg.sender_id == "subagent"
            and metadata.get("injected_event") == "subagent_result"
        ):
            marker: dict[str, Any] = {"kind": "subagent_result"}
            task_id = metadata.get("subagent_task_id")
            if isinstance(task_id, str) and task_id:
                row["subagent_task_id"] = task_id
                marker["subagent_task_id"] = task_id
            row["injected_event"] = "subagent_result"
            row[HIDDEN_HISTORY_META] = marker
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
        # step33：系统通道同样使用 replay_max_messages 进行压缩
        from step57.session.manager import replay_max_messages_for_context
        replay_max_messages = replay_max_messages_for_context(burstable.context_window_tokens)
        await self.consolidator.maybe_consolidate_by_tokens(
            session, runtime=burstable, replay_max_messages=replay_max_messages,
        )

        is_subagent = msg.sender_id == "subagent"
        if is_subagent and self._persist_subagent_followup(session, msg):
            self.sessions.save(session)
        current_role = "assistant" if is_subagent else "user"

        history = session.get_history(
            max_messages=replay_max_messages,
            max_tokens=self._replay_token_budget(burstable),
            extend_to_user=False,
        )
        # step29: 系统通道同样解析运行时上下文（provider 可依据
        # RequestContext.session_key 区分轮次）；assistant 角色不附加。
        request_context = self._build_turn_request_context(
            msg, session, key, runtime=burstable,
        )
        runtime_context_blocks = await resolve_runtime_context(
            [*self.registry.get_runtime_context_providers(),
             *self._runtime_context_providers],
            request_context,
        )
        # step34：使用 _build_initial_messages 统一构建（内部计算 goal_lines/identity/scope）。
        # 系统通道不调用 _persist_user_message_early：subagent 结果是 assistant 消息，
        # 非 subagent 系统消息通常也不需要持久化为用户输入。
        initial_messages = self._build_initial_messages(
            msg, session, history, pending,
            runtime_context_blocks=runtime_context_blocks,
            current_role=current_role,
        )

        # step27: 系统通道同样装配 bus 回调（progress / retry_wait）。
        on_progress = await self._build_bus_progress_callback(msg)
        on_retry_wait = await self._build_retry_wait_callback(msg)

        # step35：改用 _run_agent_loop（封装 _build_agent_spec + runner.run +
        # max_iterations/error 处理），对齐 nanobot 架构。
        turn_started_at = time.time()
        final_content, _, all_messages, stop_reason, _ = await self._run_agent_loop(
            initial_messages,
            msg=msg,
            session=session,
            session_key=key,
            runtime=burstable,
            pending_queue=pending_queue,
            request_context=request_context,
            on_progress=on_progress,
            on_retry_wait=on_retry_wait,
        )

        skip = len(initial_messages)
        latency_ms = max(0, int((time.time() - turn_started_at) * 1000))
        self._save_turn(session, all_messages, skip, turn_latency_ms=latency_ms)
        self._clear_pending_user_turn(session)
        self._clear_runtime_checkpoint(session)
        session.enforce_file_cap(
            on_archive=lambda chunk: self.memory.raw_archive(chunk, session_key=key)
        )
        self.sessions.save(session)
        self._schedule_background(
            self.consolidator.maybe_consolidate_by_tokens(session, runtime=burstable)
        )

        content = final_content or "Background task completed."
        return OutboundMessage(
            channel=channel,
            chat_id=chat_id,
            content=content,
            metadata={"stop_reason": stop_reason or ""},
        )

    async def _run_agent_loop(
        self,
        initial_messages: list[dict[str, Any]],
        *,
        msg: InboundMessage,
        session: Session | None,
        session_key: str,
        runtime: LLMRuntime,
        pending_queue: asyncio.Queue[InboundMessage] | None = None,
        request_context: RequestContext | None = None,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_retry_wait: Callable[[str], Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        hook_factories: list[AgentTurnHookFactory] | None = None,
        turn_scopes: list[AbstractContextManager[Any]] | None = None,
        ephemeral: bool = False,
        run_extra_hooks_for_ephemeral: bool = False,
    ) -> tuple[str | None, list[str], list[dict[str, Any]], str, bool]:
        """运行 agent 迭代循环（step35 提取，对齐 nanobot ``_run_agent_loop``）。

        封装 ``_build_agent_spec`` + ``runner.run`` + max_iterations/error 处理。
        提取后 ``_state_run`` 和 ``_process_system_message`` 共用同一运行路径。

        step40：新增 ``hook_factories``（turn 级 hook 工厂）和 ``turn_scopes``
        （turn 级 context manager，ExitStack 进入/退出）。

        step41：新增 ``ephemeral`` / ``run_extra_hooks_for_ephemeral``（临时 turn
        模式，透传到 ``_build_agent_spec`` → ``AgentTurnHookSpec``，hook 链仅保留
        progress hook）。

        Args:
            initial_messages: 初始消息列表（含 system + 历史 + 当前消息）。
            msg: 入站消息（用于构建 spec 和回调）。
            session: 当前会话；None 表示无状态运行。
            session_key: 会话 key。
            runtime: LLM 运行时。
            pending_queue: pending 消息队列；None 表示无注入。
            request_context: 富 RequestContext；None 时 ``_build_agent_spec`` 回退。
            on_progress: 进度回调。
            on_retry_wait: 重试等待回调。
            on_stream: 流式内容回调（max_iterations 收尾时推送最终内容）。
            on_stream_end: 流式结束回调。
            hook_factories: turn 级 hook 工厂（仅本 turn 有效）。
            turn_scopes: turn 级 context manager 列表，运行期间进入，结束后退出。
            ephemeral: 是否临时 turn（True 时 hook 链仅保留 progress hook）。
            run_extra_hooks_for_ephemeral: 临时 turn 是否也跑额外 hook。

        Returns:
            ``(final_content, tools_used, messages, stop_reason, had_injections)``。
        """
        # step36：同步 subagent 运行时限制（max_iterations），对齐 nanobot。
        self._sync_subagent_runtime_limits()
        # step40：turn 级 context manager，ExitStack 进入，finally 退出。
        turn_scope_stack = ExitStack()
        try:
            for scope in turn_scopes or ():
                turn_scope_stack.enter_context(scope)
            spec = self._build_agent_spec(
                msg, session_key, session, initial_messages,
                injection_callback=self._build_injection_callback(
                    pending_queue, session_key, session,
                ),
                checkpoint_callback=self._build_checkpoint_callback(session),
                retry_wait_callback=on_retry_wait,
                progress_callback=on_progress,
                request_context=request_context,
                workspace_scope=self.workspace_scopes.for_message(
                    msg, session.metadata if session else None,
                ),
                pending_budget_available=pending_queue is not None,
                hook_factories=hook_factories,
                ephemeral=ephemeral,
                run_extra_hooks_for_ephemeral=run_extra_hooks_for_ephemeral,
            )
            result = await self._runner.run(spec)

            # 记录是否按流式交付：hook 声明 wants_streaming 即视为最终内容已以
            # delta 形式流过（对齐 nanobot 用 ``on_stream`` 判定的语义）。
            # step36 修复：``spec.hook`` 是 hook 对象不可直接调用，需提取其
            # 内部 ``_on_stream`` / ``_on_stream_end`` 可调用回调；
            # ``CompositeHook`` 时遍历子 hook 找到 ``AgentProgressHook``。
            effective_stream = on_stream
            effective_stream_end = on_stream_end
            if spec.hook is not None and spec.hook.wants_streaming():
                progress_hook = spec.hook
                if not hasattr(progress_hook, "_on_stream") and hasattr(progress_hook, "_hooks"):
                    progress_hook = next(
                        (h for h in progress_hook._hooks if hasattr(h, "_on_stream")),
                        None,
                    )
                if progress_hook is not None and getattr(progress_hook, "_on_stream", None):
                    effective_stream = progress_hook._on_stream  # type: ignore[assignment]
                    effective_stream_end = progress_hook._on_stream_end  # type: ignore[assignment]

            # step35：对齐 nanobot——max_iterations 终止时，若有 stream 回调且应收尾，
            # 则通过 stream 推送最终内容（如 Feishu 卡片更新），避免卡片留空。
            if result.stop_reason == "max_iterations":
                # step36：对齐 nanobot——max_iterations 终止时记录警告日志。
                logger.warning("Max iterations (%d) reached", self.max_iterations)
                should_stream = turn_continuation.should_stream_budget_response(
                    stop_reason=result.stop_reason,
                    pending_queue_available=pending_queue is not None,
                    session_metadata=session.metadata if session else None,
                    message_metadata=msg.metadata,
                )
                if effective_stream and effective_stream_end and should_stream:
                    await effective_stream(result.final_content or "")
                    await effective_stream_end(resuming=False)
            elif result.stop_reason == "error":
                logger.error(
                    "LLM returned error: %s", (result.final_content or "")[:200],
                )

            return (
                result.final_content,
                result.tools_used,
                result.messages,
                result.stop_reason,
                result.had_injections,
            )
        finally:
            turn_scope_stack.close()

    async def _state_run(self, ctx: TurnContext) -> str:
        session_key = ctx.session_key

        # step27: 惰性构建 bus 回调（progress / retry_wait），对齐 nanobot
        # ``_state_build`` 的默认回调装配。
        if ctx.on_progress is None:
            ctx.on_progress = await self._build_bus_progress_callback(ctx.msg)
        if ctx.on_retry_wait is None:
            ctx.on_retry_wait = await self._build_retry_wait_callback(ctx.msg)
        # step29: 可见运行起点——首片取当前时刻；续跑片从 metadata 恢复
        # （turn latency 统计整个隐形 run 而非单片）。
        if ctx.visible_run_started_at is None:
            ctx.visible_run_started_at = time.time()
        # step27: 可见运行状态 + 记录该 turn 使用的 runtime（随 TurnCompleted 派发）。
        await self._runtime_events().run_status_changed(
            ctx.msg, session_key, "running",
            started_at=ctx.visible_run_started_at,
        )
        self._runtime_events().record_turn_runtime(session_key, self.runtime)

        # step35：改用 _run_agent_loop（封装 _build_agent_spec + runner.run +
        # max_iterations/error 处理），对齐 nanobot 架构。
        final_content, tools_used, all_messages, stop_reason, had_injections = (
            await self._run_agent_loop(
                ctx.initial_messages,
                msg=ctx.msg,
                session=ctx.session,
                session_key=session_key,
                runtime=self.runtime,
                pending_queue=ctx.pending_queue,
                request_context=ctx.request_context,
                on_progress=ctx.on_progress,
                on_retry_wait=ctx.on_retry_wait,
                on_stream=ctx.on_stream,
                on_stream_end=ctx.on_stream_end,
                ephemeral=ctx.ephemeral,
                run_extra_hooks_for_ephemeral=ctx.run_extra_hooks_for_ephemeral,
            )
        )
        # step57：不再重建 ctx.result，直接使用扁平字段
        # step29：续跑字段落盘到 ctx（_state_save 用）＋策略决策排班续跑片。
        ctx.final_content = final_content
        ctx.tools_used = tools_used
        ctx.all_messages = all_messages
        ctx.stop_reason = stop_reason
        ctx.had_injections = had_injections
        # step34 行为：hook 声明 wants_streaming 时设置 ctx.on_stream（_state_respond 用）。
        # _run_agent_loop 内部已构建 spec，这里重新构建一次以获取 hook（_build_agent_spec
        # 是轻量级的，ToolLoader.load 对已注册工具幂等）。
        _stream_spec = self._build_agent_spec(
            ctx.msg, session_key, ctx.session, ctx.initial_messages,
            injection_callback=self._build_injection_callback(
                ctx.pending_queue, session_key, ctx.session,
            ),
            checkpoint_callback=self._build_checkpoint_callback(ctx.session),
            retry_wait_callback=ctx.on_retry_wait,
            progress_callback=ctx.on_progress,
            request_context=ctx.request_context,
            workspace_scope=self.workspace_scopes.for_message(
                ctx.msg, ctx.session.metadata if ctx.session else None
            ),
            pending_budget_available=ctx.pending_queue is not None,
            ephemeral=ctx.ephemeral,
            run_extra_hooks_for_ephemeral=ctx.run_extra_hooks_for_ephemeral,
        )
        if _stream_spec.hook is not None and _stream_spec.hook.wants_streaming():
            ctx.on_stream = _stream_spec.hook
        await turn_continuation.maybe_continue_turn(ctx)

        # goal_continuation_rounds 同步（spec.goal_continuation_rounds 即
        # session.metadata 中的值，此处确保不回退）。
        rounds = ctx.session.metadata.get("_goal_continuation_rounds", 0) if ctx.session else 0
        if ctx.session and rounds > 0:
            ctx.session.metadata["_goal_continuation_rounds"] = rounds
        return "ok"

    async def _state_save(self, ctx: TurnContext) -> str:
        # step57：ctx.result → ctx.final_content
        if ctx.final_content is None and ctx.stop_reason not in ("error", "tool_error"):
            return "ok"
        # step29：持久化 append 边界由 turn_continuation 策略计算
        # （续跑片 / skip_user_persist / 独立 vs 合并 各形态），
        # 替换原硬编码 ``2 + len(history)``。
        turn_continuation.prepare_save_boundary(ctx)
        skip = ctx.save_skip
        latency_started_at = ctx.visible_run_started_at or ctx.turn_wall_started_at
        latency_ms = (
            max(0, int((time.time() - latency_started_at) * 1000))
            if latency_started_at
            else None
        )
        # step42：存入 ctx 供 _state_respond / _assemble_outbound 使用
        ctx.turn_latency_ms = latency_ms
        # step27: 记录 turn 延迟，随 TurnCompleted 派发给运行时事件订阅者。
        self._runtime_events().record_turn_latency(ctx.session_key, latency_ms)
        self._save_turn(
            ctx.session, ctx.all_messages, skip, turn_latency_ms=latency_ms,
        )
        self._clear_pending_user_turn(ctx.session)
        self._clear_runtime_checkpoint(ctx.session)
        # step41：ephemeral turn 跳过 enforce_file_cap（文件容量裁剪）和
        # 后台 consolidation（token 预算压缩）——临时 turn 不做长期记忆维护。
        # _save_turn 和 sessions.save 仍执行（当前 turn 消息需写入 session，
        # 元数据需持久化），对齐 nanobot _state_save 语义。
        if not ctx.ephemeral:
            ctx.session.enforce_file_cap(
                on_archive=lambda chunk: self.memory.raw_archive(
                    chunk, session_key=ctx.session_key
                )
            )
            self._schedule_background(
                self.consolidator.maybe_consolidate_by_tokens(
                    ctx.session, runtime=self.runtime,
                )
            )
        self.sessions.save(ctx.session)
        return "ok"

    def _assemble_outbound(
        self,
        msg: InboundMessage,
        final_content: str,
        all_msgs: list[dict[str, Any]],
        stop_reason: str,
        had_injections: bool,
        on_stream: Callable[[str], Awaitable[None]] | None,
        *,
        turn_latency_ms: int | None = None,
    ) -> OutboundMessage | None:
        """组装最终出站消息（step42 从 _state_respond 提取，最小增量版）。

        最小增量取舍：
        - meta 保留 stop_reason+tokens（tokens 由调用方填充），新增 latency_ms
        - 不继承 msg.metadata，不继承 msg.channel/chat_id
        - 新增 MessageTool 抑制逻辑

        Args:
            msg: 入站消息。
            final_content: 最终回复内容。
            all_msgs: 完整消息列表（当前未使用，保留对齐 nanobot 签名）。
            stop_reason: 停止原因。
            had_injections: 是否有注入消息（MessageTool 抑制的例外条件）。
            on_stream: 流式回调。
            turn_latency_ms: turn 延迟毫秒数。

        Returns:
            OutboundMessage；MessageTool 抑制时返回 None。
        """
        # step42：MessageTool 抑制——如果 MessageTool 已在本 turn 直接发送消息，
        # 且（没有注入消息 OR stop_reason 是 empty_final_response），则不重复出站。
        from step57.tools.message import MessageTool
        if (
            (mt := self.registry.get("message"))
            and isinstance(mt, MessageTool)
            and mt._sent_in_turn
        ):
            if not had_injections or stop_reason == "empty_final_response":
                return None

        meta = {
            "stop_reason": stop_reason,
            "tokens": "",  # 占位，由 _state_respond 填充实际值
        }
        # step27: 流式已交付时挂 StreamedResponseEvent
        event = None
        if on_stream is not None and stop_reason not in {"error", "tool_error"}:
            event = StreamedResponseEvent()
        # step42：新增 latency_ms
        if turn_latency_ms is not None:
            meta["latency_ms"] = int(turn_latency_ms)

        return OutboundMessage(
            content=final_content,
            metadata=meta,
            event=event,
        )

    async def _state_respond(self, ctx: TurnContext) -> str:
        # step29：续跑 pending 时抑制响应（最终响应由续跑片产出）。
        if ctx.suppress_response:
            ctx.outbound = None
            return "ok"
        # step57：ctx.result → ctx.final_content
        if ctx.final_content is None:
            ctx.outbound = OutboundMessage(content="", metadata={"stop_reason": "empty"})
            return "ok"
        # step42：改用 _assemble_outbound（提取方法 + MessageTool 抑制 + latency_ms）
        outbound = self._assemble_outbound(
            ctx.msg,
            ctx.final_content or "",
            ctx.all_messages,
            ctx.stop_reason,
            ctx.had_injections,
            ctx.on_stream,
            turn_latency_ms=ctx.turn_latency_ms,
        )
        # step57：tokens 从 ctx.usage 获取（_run_agent_loop 暂不填充，默认 0+0）
        if outbound is not None:
            outbound.metadata["tokens"] = (
                f"{ctx.usage.get('prompt_tokens', 0)}+"
                f"{ctx.usage.get('completion_tokens', 0)}"
            )
        ctx.outbound = outbound
        # step41：ephemeral turn 挂载内部 _stop_reason
        if ctx.ephemeral and ctx.outbound is not None:
            ctx.outbound.metadata["_stop_reason"] = ctx.stop_reason
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
            session.updated_at = datetime.now()

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
            # step57：image_url data: 块替换为占位文本（避免 base64 写入历史）
            if (
                block.get("type") == "image_url"
                and isinstance(block.get("image_url"), dict)
                and str(block["image_url"].get("url", "")).startswith("data:image/")
            ):
                path = (block.get("_meta") or {}).get("path", "")
                filtered.append({
                    "type": "text",
                    "text": f"[image: {path}]" if path else "[image]",
                })
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
            # step57：弹出内部 _meta，提取 runtime_context 元数据
            internal_meta = entry.pop("_meta", None)
            runtime_context_meta = (
                internal_meta.get(RUNTIME_CONTEXT_MESSAGE_META)
                if isinstance(internal_meta, dict) else None
            )
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
                # step57：将 runtime_context 元数据设置到历史消息
                if isinstance(runtime_context_meta, dict):
                    entry[RUNTIME_CONTEXT_HISTORY_META] = runtime_context_meta
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
        session.updated_at = datetime.now()

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
        """[DEPRECATED step43] 后台记忆整理（独立路径）。

        .. deprecated::
            将在 harness 阶段（step58）迁移到 ``process_direct(ephemeral=True)``。
            当前保留用于向后兼容。
        """
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

    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        *,
        channel: str = "cli",
        chat_id: str = "direct",
        ephemeral: bool = False,
        run_extra_hooks_for_ephemeral: bool = False,
        runtime: LLMRuntime | None = None,
    ) -> OutboundMessage | None:
        """直接处理一条消息并返回出站载荷（step43 最小增量版）。

        绕过 bus，直接构建 InboundMessage 并调用 ``_process_message`` 状态机。
        为 harness dream/heartbeat 从 ``run_dream`` 迁移铺路。

        最小增量：只支持 content/session_key/channel/chat_id/ephemeral/
        run_extra_hooks_for_ephemeral/runtime。hooks/hook_factories/tools/
        on_progress/on_stream/persist_user_message/media 留到后续扩展。

        Args:
            content: 消息内容。
            session_key: 会话 key，默认 "cli:direct"。
            channel: 通道名，默认 "cli"。
            chat_id: 聊天 ID，默认 "direct"。
            ephemeral: 是否临时 turn（不做长期记忆维护）。
            run_extra_hooks_for_ephemeral: ephemeral 时是否也跑额外 hook。
            runtime: 自定义 LLM 运行时，None 用默认。

        Returns:
            OutboundMessage 或 None（suppress_response 时）。
        """
        msg = InboundMessage(
            channel=channel,
            chat_id=chat_id,
            content=content,
        )
        # 复用 _session_locks，确保 direct 调用与 bus turn 串行化
        lock = self._session_locks.setdefault(session_key, asyncio.Lock())
        try:
            async with lock:
                return await self._process_message(
                    msg,
                    session_key,
                    ephemeral=ephemeral,
                    run_extra_hooks_for_ephemeral=run_extra_hooks_for_ephemeral,
                    runtime=runtime,
                )
        finally:
            # 对齐 nanobot：direct 调用结束后重置 run_status
            await self._runtime_events().run_status_changed(msg, session_key, "idle")
