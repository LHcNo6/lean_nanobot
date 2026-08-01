from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class InboundMessage:
    content: str
    channel: str = "cli"
    sender_id: str = ""
    chat_id: str = "default"
    timestamp: datetime = field(default_factory=datetime.now)
    session_key: str | None = None
    session_key_override: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class OutboundMessage:
    content: str
    channel: str = "cli"
    chat_id: str = "default"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class StreamDeltaEvent(OutboundMessage):
    finished: bool = False
    session_key: str | None = None
