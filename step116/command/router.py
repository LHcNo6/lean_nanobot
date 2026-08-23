"""Minimal command routing table for slash commands.

最小移植 `nanobot/command/router.py`：三档路由（priority / exact / prefix）、
`normalize_command_text`、`CommandContext`。priority 档保留 API 但暂无
消费方（无 /stop 类命令），exact/prefix 由 loop 的 COMMAND 状态使用。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable

if TYPE_CHECKING:
    from step116.bus.events import InboundMessage, OutboundMessage
    from step116.session import Session

Handler = Callable[["CommandContext"], Awaitable["OutboundMessage | None"]]
_BOT_SUFFIX_RE = re.compile(r"^[A-Za-z0-9_]+$")


def normalize_command_text(text: str) -> str:
    """Normalize slash-command transport variants before routing.

    Telegram / Discord 风格分发会产生 ``/cmd@bot args``，bot 后缀属于传输层
    而非命令名，因此在路由边界剥离一次，用户参数原样保留。
    """
    stripped = text.strip()
    if not stripped.startswith("/"):
        return stripped
    first, sep, rest = stripped.partition(" ")
    if "@" not in first:
        return stripped
    command, suffix = first.rsplit("@", 1)
    if command and suffix and _BOT_SUFFIX_RE.fullmatch(suffix):
        return f"{command}{sep}{rest}" if sep else command
    return stripped


@dataclass
class CommandContext:
    """A command handler needs everything to produce a response."""

    msg: "InboundMessage"
    session: "Session | None"
    key: str
    raw: str
    args: str = ""
    loop: Any = None


class CommandRouter:
    """Pure dict-based command dispatch.

    Two tiers checked in order:
      1. *exact* — exact-match commands (e.g. "/history").
      2. *prefix* — longest-prefix-first match (e.g. "/pairing").
    """

    def __init__(self) -> None:
        self._priority: dict[str, Handler] = {}
        self._exact: dict[str, Handler] = {}
        self._prefix: list[tuple[str, Handler]] = []

    def priority(self, cmd: str, handler: Handler) -> None:
        self._priority[cmd] = handler

    def exact(self, cmd: str, handler: Handler) -> None:
        self._exact[cmd] = handler

    def prefix(self, pfx: str, handler: Handler) -> None:
        self._prefix.append((pfx, handler))
        self._prefix.sort(key=lambda p: len(p[0]), reverse=True)

    def is_priority(self, text: str) -> bool:
        return normalize_command_text(text).lower() in self._priority

    def is_dispatchable_command(self, text: str) -> bool:
        """Check whether *text* matches any non-priority command tier (exact or prefix).

        If this returns True, ``dispatch()`` is guaranteed to match a handler.
        """
        cmd = normalize_command_text(text).lower()
        if cmd in self._exact:
            return True
        for pfx, _ in self._prefix:
            if cmd.startswith(pfx):
                return True
        return False

    async def dispatch_priority(self, ctx: CommandContext) -> "OutboundMessage | None":
        """Dispatch a priority command (called without the session lock)."""
        ctx.raw = normalize_command_text(ctx.raw)
        handler = self._priority.get(ctx.raw.lower())
        if handler:
            return await handler(ctx)
        return None

    async def dispatch(self, ctx: CommandContext) -> "OutboundMessage | None":
        """Try exact, then prefix handlers. Returns None if unhandled."""
        ctx.raw = normalize_command_text(ctx.raw)
        cmd = ctx.raw.lower()

        if handler := self._exact.get(cmd):
            return await handler(ctx)

        for pfx, handler in self._prefix:
            if cmd.startswith(pfx):
                ctx.args = ctx.raw[len(pfx):]
                return await handler(ctx)

        return None
