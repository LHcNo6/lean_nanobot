"""消息总线公共入口。

step27 把原顶层 ``bus.py`` / ``events.py`` 收纳为 ``bus/`` 包（对齐 nanobot
``nanobot/bus/`` 布局）：

- ``bus/queue.py``      — MessageBus（inbound/outbound 双队列）
- ``bus/events.py``     — InboundMessage / OutboundMessage (含 ``event`` 字段)
- ``bus/outbound_events.py`` — typed outbound events + 工厂
- ``bus/runtime_events.py` — 进程内 RuntimeEventBus（pub/sub）
- ``bus/progress.py``   — progress 回调 helper

本模块只做重导出，保证 ``from step74.bus import MessageBus`` 等旧引用不变。
"""

from __future__ import annotations

from step74.bus.events import InboundMessage, OutboundMessage, StreamDeltaEvent
from step74.bus.queue import MessageBus

__all__ = [
    "InboundMessage",
    "MessageBus",
    "OutboundMessage",
    "StreamDeltaEvent",
]