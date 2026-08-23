"""Typed outbound events carried by :class:`OutboundMessage`.

对齐 ``nanobot/bus/outbound_events.py``。消息总线仍然运输
:class:`OutboundMessage`（通道需要 chat 路由字段），运行时/UI 语义挂在
消息的 ``event`` 字段上，而不是保留在 metadata 魔法 flag 里。

本 step 提供 6 个事件类型（roadmap H4）：
- ``ProgressEvent``          — 进度（工具执行等）
- ``RetryWaitEvent``         — provider 重试等待心跳
- ``StreamEndEvent``         — 流式结束（含 resuming 续流）
- ``StreamedResponseEvent``  — 最终流式响应完成
- ``TurnEndEvent``           — turn 结束（本 step 只定义，不产出；真实通道
  step 由通道生成，对齐 nanobot websocket/webui_turns）
- ``GoalStatusEvent``        — 目标状态变化（同 TurnEndEvent，只定义）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from step122.bus.events import OutboundMessage


class OutboundEvent:
    """Marker base for internal outbound runtime events."""


@dataclass(frozen=True)
class ProgressEvent(OutboundEvent):
    """工具/推理进度。字段对齐 nanobot；``tool_hint`` / ``tool_events`` /
    ``file_edit_events`` / ``reasoning*`` 在 step32 hook 体系补齐后才会置位。"""

    content: str = ""
    tool_hint: bool = False
    reasoning: bool = False
    reasoning_delta: bool = False
    reasoning_end: bool = False
    stream_id: str | None = None
    tool_events: list[dict[str, Any]] | None = None
    file_edit_events: list[dict[str, Any]] | None = None


@dataclass(frozen=True)
class RetryWaitEvent(OutboundEvent):
    """provider 重试等待心跳（如 "Model request failed, retry in 3s"）。"""

    content: str = ""


@dataclass(frozen=True)
class StreamDeltaEvent(OutboundEvent):
    """流式内容增量（step64：对齐 nanobot typed event，支持 stream_id 分段）。"""

    content: str = ""
    stream_id: str | None = None


@dataclass(frozen=True)
class StreamEndEvent(OutboundEvent):
    """流式响应结束。``resuming`` 表示这是恢复续流后的段。"""

    content: str = ""
    stream_id: str | None = None
    resuming: bool = False


@dataclass(frozen=True)
class StreamedResponseEvent(OutboundEvent):
    """最终响应以流式方式交付（挂在最终 OutboundMessage.event 上）。"""

    pass


@dataclass(frozen=True)
class TurnEndEvent(OutboundEvent):
    """turn 结束（真实通道步由通道/渲染层产出，本 step 仅定义类型）。"""

    latency_ms: int | None = None
    goal_state: dict[str, Any] | None = None


@dataclass(frozen=True)
class GoalStatusEvent(OutboundEvent):
    """目标状态变化（同 TurnEndEvent，仅定义类型）。"""

    status: str
    started_at: float | None = None


def outbound_message_for_event(
    *,
    channel: str,
    chat_id: str,
    event: OutboundEvent,
    content: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> OutboundMessage:
    """Build an :class:`OutboundMessage` for a typed event.

    content 缺省时从事件推导（Progress/RetryWait/StreamEnd 携带内容，
    其余事件为 ""）。metadata 原样透传（路由上下文）。
    """

    return OutboundMessage(
        channel=channel,
        chat_id=chat_id,
        content=_event_content(event) if content is None else content,
        event=event,
        metadata=dict(metadata or {}),
    )


def _event_content(event: OutboundEvent) -> str:
    """从事件推导默认出站内容（对齐 nanobot ``_event_content``）。"""

    if isinstance(event, (ProgressEvent, RetryWaitEvent, StreamEndEvent)):
        return event.content
    return ""