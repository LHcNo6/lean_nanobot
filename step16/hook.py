from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class AgentHookContext:
    iteration: int
    messages: list[dict[str, Any]]
    session_key: str | None = None
    response: Any | None = None
    usage: dict[str, int] = field(default_factory=dict)
    tool_calls: list[Any] = field(default_factory=list)
    tool_results: list[Any] = field(default_factory=list)
    final_content: str | None = None
    stop_reason: str | None = None
    error: str | None = None
    stream_content: str = ""


@dataclass
class AgentRunHookContext:
    messages: list[dict[str, Any]]
    final_content: str | None = None
    tools_used: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    stop_reason: str | None = None
    error: str | None = None
    exception: BaseException | None = None


class AgentHook:
    async def before_run(self, context: AgentRunHookContext) -> None:
        ...

    async def after_run(self, context: AgentRunHookContext) -> None:
        ...

    async def on_error(self, context: AgentRunHookContext) -> None:
        ...

    async def on_finally(self, context: AgentRunHookContext) -> None:
        ...

    async def before_iteration(self, context: AgentHookContext) -> None:
        ...

    async def after_iteration(self, context: AgentHookContext) -> None:
        ...

    async def on_stream(self, context: AgentHookContext, delta: str) -> None:
        ...

    async def on_stream_end(self, context: AgentHookContext) -> None:
        ...


class CompositeHook(AgentHook):
    def __init__(self, hooks: list[AgentHook]) -> None:
        super().__init__()
        self._hooks = list(hooks)

    async def _for_each(self, method: str, context: Any) -> None:
        for hook in self._hooks:
            try:
                handler = getattr(hook, method)
                await handler(context)
            except Exception as exc:
                logger.exception("Hook %s.%s failed: %s", type(hook).__name__, method, exc)

    async def before_run(self, context: AgentRunHookContext) -> None:
        await self._for_each("before_run", context)

    async def after_run(self, context: AgentRunHookContext) -> None:
        await self._for_each("after_run", context)

    async def on_error(self, context: AgentRunHookContext) -> None:
        await self._for_each("on_error", context)

    async def on_finally(self, context: AgentRunHookContext) -> None:
        await self._for_each("on_finally", context)

    async def before_iteration(self, context: AgentHookContext) -> None:
        await self._for_each("before_iteration", context)

    async def after_iteration(self, context: AgentHookContext) -> None:
        await self._for_each("after_iteration", context)

    async def on_stream(self, context: AgentHookContext, delta: str) -> None:
        for hook in self._hooks:
            try:
                await hook.on_stream(context, delta)
            except Exception as exc:
                logger.exception("Hook %s.on_stream failed: %s", type(hook).__name__, exc)

    async def on_stream_end(self, context: AgentHookContext) -> None:
        for hook in self._hooks:
            try:
                await hook.on_stream_end(context)
            except Exception as exc:
                logger.exception("Hook %s.on_stream_end failed: %s", type(hook).__name__, exc)
