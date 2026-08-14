from __future__ import annotations

import asyncio
from typing import Any, Callable

from step35.channel import BaseChannel
from step35.bus.events import OutboundMessage
from step35.bus.outbound_events import ProgressEvent, RetryWaitEvent


async def ainput(prompt: str = "") -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, input, prompt)


class CliChannel(BaseChannel):
    """First channel implementation: a readline REPL routed through the bus.

    The local operator is always allowed (default ``allow_from: ["*"]``,
    mirroring nanobot's interactive CLI which bypasses permission checks).
    """

    name = "cli"
    display_name = "CLI"

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        bus=None,
        pairing=None,
        on_command: Callable[[str], bool] | None = None,
        chat_id: str = "default",
    ) -> None:
        super().__init__(config, bus, pairing)
        self.on_command = on_command
        self.chat_id = chat_id
        self._turn_done = asyncio.Event()
        self._buffers: dict[tuple[str, str], list[str]] = {}

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return {"enabled": True, "allow_from": ["*"], "streaming": True}

    async def start(self) -> None:
        self._running = True
        while self._running:
            text = await ainput("You: ")
            if not text:
                continue
            if text.lower() == "/exit":
                await self.stop()
                break
            if self.on_command and await self.on_command(text):
                continue
            self._turn_done.clear()
            await self._handle_message("user", self.chat_id, text)
            await self._turn_done.wait()

    async def stop(self) -> None:
        self._running = False

    async def send(self, msg: OutboundMessage) -> None:
        # step27: typed 运行时事件（进度/重试等待）不是最终回复——只作状态行
        # 打印、不结束 turn，避免破坏 _turn_done 的"等待最终响应"语义。
        if isinstance(msg.event, (ProgressEvent, RetryWaitEvent)):
            if self.send_progress and msg.content:
                print(f"  · {msg.content}", flush=True)
            return
        stop_reason = msg.metadata.get("stop_reason", "?")
        print(f"\n[{stop_reason}]", flush=True)
        print(f"{msg.content}", flush=True)
        tokens = msg.metadata.get("tokens", "?")
        print(f"  tokens: {tokens}", flush=True)
        print(flush=True)
        self._turn_done.set()

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
        key = (str(chat_id), str(stream_id or ""))
        if stream_end:
            buffered = self._buffers.pop(key, [])
            if delta:
                buffered.append(delta)
            full = "".join(buffered)
            if full:
                print(full, flush=True)
        else:
            self._buffers.setdefault(key, []).append(delta)
