from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from step123.bus import MessageBus
from step123.bus.events import InboundMessage, OutboundMessage
from step123.pairing import PAIRING_CODE_META_KEY, PairingStore


class BaseChannel(ABC):
    """Abstract base class for chat channel implementations."""

    name: str = "base"
    display_name: str = "Base"
    send_progress: bool = True
    send_tool_hints: bool = False
    show_reasoning: bool = True

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        bus: MessageBus | None = None,
        pairing: PairingStore | None = None,
    ) -> None:
        self.config = config or {}
        self.bus = bus
        self.pairing = pairing or PairingStore()
        self._running = False

    @abstractmethod
    async def start(self) -> None:
        """Start the channel and begin listening for messages."""
        raise NotImplementedError

    @abstractmethod
    async def stop(self) -> None:
        """Stop the channel and clean up resources."""
        raise NotImplementedError

    @abstractmethod
    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through this channel.

        Implementations should raise on delivery failure so the channel
        manager can apply its retry policy in one place.
        """
        raise NotImplementedError

    async def send_delta(
        self,
        chat_id: str,
        delta: str,
        metadata: dict[str, Any] | None = None,
        *,
        stream_id: str | None = None,
        stream_end: bool = False,
        resuming: bool = False,
    ) -> None:
        """Deliver a streaming text chunk.

        Default is no-op. Override in subclasses to enable streaming.
        Stateful implementations should key buffers by ``stream_id``
        rather than only by ``chat_id`` when it is provided.
        """
        return

    @property
    def supports_streaming(self) -> bool:
        """True when config enables streaming AND this subclass implements send_delta."""
        return bool(self.config.get("streaming", False)) and type(self).send_delta is not BaseChannel.send_delta

    def is_allowed(self, sender_id: str) -> bool:
        """Check sender permission: star > allowFrom > pairing store > deny."""
        allow_list = self.config.get("allow_from") or self.config.get("allowFrom") or []
        if "*" in allow_list:
            return True
        if str(sender_id) in allow_list:
            return True
        return self.pairing.is_approved(self.name, str(sender_id))

    async def _handle_message(
        self,
        sender_id: str,
        chat_id: str,
        content: str,
        media: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        session_key: str | None = None,
        is_dm: bool = False,
    ) -> None:
        """Handle an incoming message: check permissions, issue pairing codes in DMs, or forward to bus."""
        if not self.is_allowed(sender_id):
            if is_dm:
                code = self.pairing.generate_code(self.name, str(sender_id))
                await self.send(
                    OutboundMessage(
                        channel=self.name,
                        chat_id=str(chat_id),
                        content=self.pairing.format_pairing_reply(code),
                        metadata={PAIRING_CODE_META_KEY: code},
                    )
                )
                print(
                    f"[{self.name}] Sent pairing code {code} to sender {sender_id} in chat {chat_id}"
                )
            else:
                print(
                    f"[{self.name}] Access denied for sender {sender_id}. "
                    "Add them to allowFrom list in config to grant access."
                )
            return

        meta = dict(metadata or {})
        if self.supports_streaming:
            meta["_wants_stream"] = True

        msg = InboundMessage(
            channel=self.name,
            sender_id=str(sender_id),
            chat_id=str(chat_id),
            content=content,
            media=media or [],
            metadata=meta,
            session_key_override=session_key,
        )
        await self.bus.publish_inbound(msg)

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        """Return default config for a channel."""
        return {"enabled": False}

    @property
    def is_running(self) -> bool:
        """Check if the channel is running."""
        return self._running
