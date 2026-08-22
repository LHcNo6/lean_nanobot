from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any, Callable

from step100.bus import MessageBus
from step100.bus.events import OutboundMessage, StreamDeltaEvent
from step100.bus.outbound_events import (
    ProgressEvent,
    RetryWaitEvent,
    StreamEndEvent,
)
from step100.channel import BaseChannel
from step100.channels.registry import (
    DEFAULT_ENABLED_CHANNELS,
    discover_channel_names,
    load_channel_class,
)
from step100.pairing import PairingStore

_SEND_RETRY_DELAYS = (1, 2, 4)
_SEND_MAX_RETRIES = 3


class ChannelManager:
    """Manage chat channels: discovery, init, start/stop, outbound routing."""

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        bus: MessageBus | None = None,
        pairing: PairingStore | None = None,
        on_command: Callable[[str], bool] | None = None,
    ) -> None:
        self.config = config or {}
        self.bus = bus or MessageBus()
        self.pairing = pairing or PairingStore()
        self.on_command = on_command
        self.channels: dict[str, BaseChannel] = {}
        self._channel_tasks: dict[str, asyncio.Task] = {}
        self._dispatch_task: asyncio.Task | None = None
        self._started = False
        self._init_channels()

    def _init_channels(self) -> None:
        """Initialize channels discovered via pkgutil scan plus config sections."""
        names = discover_channel_names()
        candidates = set(names) | set(self.config)
        for name in sorted(candidates):
            section = self.config.get(name)
            if section is None and name not in DEFAULT_ENABLED_CHANNELS:
                continue
            section = dict(section or {})
            if not section.get("enabled", name in DEFAULT_ENABLED_CHANNELS):
                continue
            if name not in names:
                print(f"[manager] Channel '{name}' not found in step64.channels, skipping")
                continue
            try:
                cls = load_channel_class(name)
            except Exception as e:
                print(f"[manager] Channel '{name}' not available: {e}")
                continue
            try:
                channel = cls(section, self.bus, pairing=self.pairing)
            except Exception as e:
                print(f"[manager] Channel '{name}' failed to initialize: {e}")
                continue
            if self.on_command is not None and hasattr(channel, "on_command"):
                channel.on_command = self.on_command
            self.channels[name] = channel
            print(f"[manager] {channel.display_name} channel enabled")

    async def _start_channel(self, name: str, channel: BaseChannel) -> None:
        try:
            await channel.start()
        except Exception:
            print(f"[manager] Failed to start channel {name}")

    async def _stop_channel(self, name: str) -> bool:
        channel = self.channels.get(name)
        if channel is None:
            self._channel_tasks.pop(name, None)
            return False
        task = self._channel_tasks.pop(name, None)
        try:
            await channel.stop()
        except Exception:
            print(f"[manager] Error stopping {name}")
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        return True

    async def start_all(self) -> None:
        """Start all channels and the outbound dispatcher, then wait for them to stop."""
        if not self.channels:
            print("[manager] No channels enabled")
            return
        self._started = True
        self._dispatch_task = asyncio.create_task(self._dispatch_outbound())
        tasks = [
            asyncio.create_task(self._start_channel(name, channel))
            for name, channel in self.channels.items()
        ]
        self._channel_tasks.update(
            {name: task for name, task in zip(self.channels, tasks)}
        )
        await asyncio.gather(*tasks, return_exceptions=True)

    async def stop_all(self) -> None:
        """Stop the dispatcher and all channels."""
        self._started = False
        if self._dispatch_task:
            self._dispatch_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._dispatch_task
        for name in list(self.channels):
            await self._stop_channel(name)

    async def _dispatch_outbound(self) -> None:
        """Route outbound messages to the appropriate channel."""
        while True:
            try:
                msg = await asyncio.wait_for(self.bus.consume_outbound(), timeout=1.0)
                channel = self.channels.get(msg.channel)
                if channel is None:
                    print(f"[manager] Unknown channel: {msg.channel}")
                    continue
                if isinstance(msg, StreamDeltaEvent):
                    # legacy 流式增量（step25 形态，保留兼容）
                    if msg.finished:
                        await channel.send_delta(
                            msg.chat_id, "", msg.metadata, stream_end=True
                        )
                    else:
                        await channel.send_delta(
                            msg.chat_id, msg.content, msg.metadata
                        )
                elif isinstance(msg.event, StreamEndEvent):
                    # typed 流式结束 → 以 stream_end 语义投递
                    await channel.send_delta(
                        msg.chat_id,
                        msg.event.content,
                        msg.metadata,
                        stream_id=msg.event.stream_id,
                        stream_end=True,
                        resuming=msg.event.resuming,
                    )
                elif isinstance(msg.event, (ProgressEvent, RetryWaitEvent)):
                    # 运行时状态/进度：有内容才转发，由通道决定是否展示
                    if msg.event.content:
                        await self._send_with_retry(channel, msg)
                else:
                    await self._send_with_retry(channel, msg)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

    async def _send_with_retry(self, channel: BaseChannel, msg: OutboundMessage) -> None:
        """Send a message with retry on failure using exponential backoff (1s, 2s, 4s)."""
        attempt = 0
        while True:
            attempt += 1
            try:
                await channel.send(msg)
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if attempt >= _SEND_MAX_RETRIES:
                    print(
                        f"[manager] Failed to send to {msg.channel} after "
                        f"{attempt} attempts: {type(e).__name__}: {e}"
                    )
                    return
                delay = _SEND_RETRY_DELAYS[min(attempt - 1, len(_SEND_RETRY_DELAYS) - 1)]
                print(
                    f"[manager] Send to {msg.channel} failed (attempt "
                    f"{attempt}/{_SEND_MAX_RETRIES}): {type(e).__name__}, "
                    f"retrying in {delay}s"
                )
                await asyncio.sleep(delay)

    def get_channel(self, name: str) -> BaseChannel | None:
        """Get a channel by name."""
        return self.channels.get(name)

    def get_status(self) -> dict[str, dict[str, Any]]:
        """Get status of all channels."""
        return {
            name: {"enabled": True, "running": channel.is_running}
            for name, channel in self.channels.items()
        }

    @property
    def enabled_channels(self) -> list[str]:
        """Get list of enabled channel names."""
        return list(self.channels.keys())
