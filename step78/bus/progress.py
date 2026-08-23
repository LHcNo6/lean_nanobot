"""Progress callback helpers for user-visible output.

对齐 ``nanobot/bus/progress.py``：把 agent 的进度回调转换成 outbound 消息
（ProgressEvent）。turn 生命周期与模型变更等运行时状态通知在
``bus/runtime_events.py``。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from step78.bus.events import InboundMessage
from step78.bus.outbound_events import ProgressEvent, outbound_message_for_event
from step78.bus.queue import MessageBus


def build_bus_progress_callback(
    bus: MessageBus,
    msg: InboundMessage,
) -> Callable[..., Awaitable[None]]:
    """返回一个把进度发布为 outbound 消息的回调。

    回调签名对齐 nanobot：``content`` 必填，其余关键字可选（``tool_hint`` /
    ``tool_events`` / ``file_edit_events`` / ``reasoning`` / ``reasoning_end``）；
    runner 通过 ``inspect.signature`` 探测后再调用，旧式回调（只收 content）
    也能被复用。
    """

    async def _publish_progress(
        content: str,
        *,
        tool_hint: bool = False,
        tool_events: list[dict[str, Any]] | None = None,
        file_edit_events: list[dict[str, Any]] | None = None,
        reasoning: bool = False,
        reasoning_end: bool = False,
    ) -> None:
        await bus.publish_outbound(
            outbound_message_for_event(
                channel=msg.channel,
                chat_id=msg.chat_id,
                event=ProgressEvent(
                    content=content,
                    tool_hint=tool_hint,
                    reasoning_delta=reasoning,
                    reasoning_end=reasoning_end,
                    tool_events=tool_events,
                    file_edit_events=file_edit_events,
                ),
                metadata=msg.metadata,
            )
        )

    async def _bus_progress(
        content: str,
        *,
        tool_hint: bool = False,
        tool_events: list[dict[str, Any]] | None = None,
        file_edit_events: list[dict[str, Any]] | None = None,
        reasoning: bool = False,
        reasoning_end: bool = False,
    ) -> None:
        await _publish_progress(
            content,
            tool_hint=tool_hint,
            tool_events=tool_events,
            file_edit_events=file_edit_events,
            reasoning=reasoning,
            reasoning_end=reasoning_end,
        )

    return _bus_progress