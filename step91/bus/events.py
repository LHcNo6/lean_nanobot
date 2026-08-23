"""总线消息类型（对齐 ``nanobot/bus/events.py``）。

- ``InboundMessage``：通道 → agent 的入站消息。
- ``OutboundMessage``：agent → 通道的出站消息。``event`` 字段携带 typed
  outbound event（见 ``bus/outbound_events.py``），metadata 只保留路由信息。
- ``StreamDeltaEvent``：legacy 流式增量消息（typed 事件之前的形态），保留
  以兼容 step25 的流式路径；新代码优先使用 typed events。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from step91.bus.outbound_events import OutboundEvent


@dataclass
class InboundMessage:
    """Message received from a chat channel."""

    content: str
    channel: str = "cli"
    sender_id: str = ""
    chat_id: str = "default"
    timestamp: datetime = field(default_factory=datetime.now)
    session_key: str | None = None
    session_key_override: str | None = None
    media: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class OutboundMessage:
    """Message to send to a chat channel.

    ``event`` 携带 typed outbound runtime event（progress / retry wait /
    stream end 等），通道可据此决定渲染语义；``metadata`` 保留路由信息。
    """

    content: str
    channel: str = "cli"
    chat_id: str = "default"
    metadata: dict[str, Any] = field(default_factory=dict)
    event: "OutboundEvent | None" = None


@dataclass
class StreamDeltaEvent(OutboundMessage):
    """Legacy streaming delta message (pre-typed-events).

    Kept for backward compatibility: the manager dispatches this subclass
    directly to ``send_delta``. New code should prefer typed events in
    ``bus/outbound_events.py`` (e.g. ``StreamEndEvent``).
    """

    finished: bool = False
    session_key: str | None = None