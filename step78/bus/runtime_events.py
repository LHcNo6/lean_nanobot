"""Runtime event bus for agent state notifications.

对齐 ``nanobot/bus/runtime_events.py``。此总线独立于消息总线
（``bus/queue.py``）：消息总线负责用户/聊天交付，运行时事件是进程内的状态
通知，可选订阅者（如 WebUI 适配器、CLI 演示）可以渲染它们。

本 step 提供 3 个事件（roadmap H4 子集）：
- ``SessionTurnStarted``     — 会话 turn 已加载 session，即将构建上下文
- ``TurnRunStatusChanged``   — turn 的可见运行状态变化（running / idle）
- ``TurnCompleted``          — turn 已交付最终用户可见响应
（``GoalStateChanged`` / ``RuntimeModelChanged`` 留待后续 step）
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from step78.bus.events import InboundMessage

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RuntimeEventContext:
    """Routing context common to turn-scoped runtime events."""

    channel: str
    chat_id: str
    session_key: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SessionTurnStarted:
    """A user/system turn has loaded its session and is about to build context."""

    context: RuntimeEventContext


@dataclass(frozen=True)
class TurnRunStatusChanged:
    """Visible run status changed for a turn."""

    context: RuntimeEventContext
    status: str
    started_at: float | None = None


@dataclass(frozen=True)
class TurnCompleted:
    """A turn has delivered its final user-visible response."""

    context: RuntimeEventContext
    latency_ms: int | None = None
    runtime: Any | None = None


RuntimeEvent = SessionTurnStarted | TurnRunStatusChanged | TurnCompleted
RuntimeEventType = (
    type[SessionTurnStarted]
    | type[TurnRunStatusChanged]
    | type[TurnCompleted]
)
RuntimeEventHandler = Callable[[Any], Awaitable[None] | None]
_HandlerEntry = tuple[RuntimeEventType | None, RuntimeEventHandler]


class RuntimeEventBus:
    """Small in-process pub/sub bus for runtime state.

    订阅者按注册顺序执行；``publish`` 会 await 异步 handler，保证运行时事件
    可严格跟随用户消息排布；``publish_nowait`` 供同步调用点使用（需要存在
    运行中的事件循环，否则丢弃并记 debug 日志）。handler 抛异常不影响其它
    订阅者（吞掉并记日志）。
    """

    def __init__(self) -> None:
        self._handlers: list[_HandlerEntry] = []

    def subscribe(
        self,
        handler: RuntimeEventHandler,
        event_type: RuntimeEventType | None = None,
    ) -> Callable[[], None]:
        """注册订阅者；返回退订函数。event_type 为 None 接收全部事件。"""

        entry = (event_type, handler)
        self._handlers.append(entry)

        def _unsubscribe() -> None:
            with contextlib.suppress(ValueError):
                self._handlers.remove(entry)

        return _unsubscribe

    async def publish(self, event: RuntimeEvent) -> None:
        """按注册顺序把事件派发给匹配的订阅者并等待其完成。"""

        for event_type, handler in list(self._handlers):
            if event_type is not None and not isinstance(event, event_type):
                continue
            try:
                result = handler(event)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.exception(
                    "runtime event handler failed for %s", type(event).__name__
                )

    def publish_nowait(self, event: RuntimeEvent) -> None:
        """无 await 场景：把事件调度到后台任务发布。"""

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug(
                "dropping runtime event without a running loop: %s",
                type(event).__name__,
            )
            return
        loop.create_task(self.publish(event))


class RuntimeEventPublisher:
    """Convenience publisher for turn-scoped runtime events.

    Agent 代码负责决定状态转移时机；本 helper 只负责构建事件上下文与携带
    per-turn metadata（latency / runtime）。event emitter 在 turn 结束时被
    取出并随 ``TurnCompleted`` 一起派发。
    """

    def __init__(self, bus: RuntimeEventBus | None = None) -> None:
        self.bus = bus or RuntimeEventBus()
        self._turn_latency_ms: dict[str, int] = {}
        self._turn_runtime: dict[str, Any] = {}

    @staticmethod
    def _context(
        *,
        channel: str,
        chat_id: str,
        session_key: str,
        metadata: dict[str, Any] | None,
    ) -> RuntimeEventContext:
        return RuntimeEventContext(
            channel=channel,
            chat_id=chat_id,
            session_key=session_key,
            metadata=dict(metadata or {}),
        )

    def record_turn_runtime(self, session_key: str, runtime: Any) -> None:
        """记录该 turn 使用的 LLMRuntime，随 TurnCompleted 一并派发。"""

        self._turn_runtime[session_key] = runtime

    def record_turn_latency(self, session_key: str, latency_ms: int | None) -> None:
        """记录该 turn 的墙钟延迟，随 TurnCompleted 一并派发。"""

        if latency_ms is not None:
            self._turn_latency_ms[session_key] = int(latency_ms)

    def clear_turn(self, session_key: str) -> None:
        """清理 turn 级暂存（延迟/运行时），在 run_status idle 时调用。"""

        self._turn_latency_ms.pop(session_key, None)
        self._turn_runtime.pop(session_key, None)

    async def session_turn_started(
        self,
        msg: InboundMessage,
        session_key: str,
    ) -> None:
        """派发 SessionTurnStarted。"""

        await self.bus.publish(
            SessionTurnStarted(
                context=self._context(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    session_key=session_key,
                    metadata=msg.metadata,
                )
            )
        )

    async def run_status_changed(
        self,
        msg: InboundMessage,
        session_key: str,
        status: str,
        *,
        started_at: float | None = None,
    ) -> None:
        """派发 TurnRunStatusChanged（running / idle 等可见状态）。"""

        await self.bus.publish(
            TurnRunStatusChanged(
                context=self._context(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    session_key=session_key,
                    metadata=msg.metadata,
                ),
                status=status,
                started_at=started_at,
            )
        )

    async def turn_completed(
        self,
        *,
        channel: str,
        chat_id: str,
        session_key: str,
        metadata: dict[str, Any] | None,
    ) -> None:
        """派发 TurnCompleted；取出并弹出该 turn 记录的 latency/runtime。"""

        await self.bus.publish(
            TurnCompleted(
                context=self._context(
                    channel=channel,
                    chat_id=chat_id,
                    session_key=session_key,
                    metadata=metadata,
                ),
                latency_ms=self._turn_latency_ms.pop(session_key, None),
                runtime=self._turn_runtime.pop(session_key, None),
            )
        )


def ensure_runtime_event_publisher(owner: Any) -> RuntimeEventPublisher:
    """返回 owner 的运行时发布器；缺失时惰性创建并挂到 owner 上。

    对齐 nanobot 同名函数：优先复用 ``owner.runtime_event_publisher``，
    否则在 ``owner.runtime_events`` 上建 ``RuntimeEventBus`` 并包装。
    """

    publisher = getattr(owner, "runtime_event_publisher", None)
    if isinstance(publisher, RuntimeEventPublisher):
        return publisher

    bus = getattr(owner, "runtime_events", None)
    if not isinstance(bus, RuntimeEventBus):
        bus = RuntimeEventBus()
        owner.runtime_events = bus

    publisher = RuntimeEventPublisher(bus)
    owner.runtime_event_publisher = publisher
    return publisher