from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


# ---- Tool Context (Step 18) ----

_CURRENT_REQUEST_CONTEXT: ContextVar["RequestContext | None"] = ContextVar(
    "tool_request_context",
    default=None,
)


@dataclass(frozen=True)
class RequestContext:
    channel: str = ""
    chat_id: str = ""
    message_id: str | None = None
    session_key: str | None = None
    original_user_text: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class ContextAware(Protocol):
    def set_context(self, ctx: RequestContext) -> None:
        ...


def bind_request_context(ctx: RequestContext) -> Token[RequestContext | None]:
    return _CURRENT_REQUEST_CONTEXT.set(ctx)


def reset_request_context(token: Token[RequestContext | None]) -> None:
    _CURRENT_REQUEST_CONTEXT.reset(token)


@contextmanager
def request_context(ctx: RequestContext):
    token = bind_request_context(ctx)
    try:
        yield ctx
    finally:
        reset_request_context(token)


def current_request_context() -> RequestContext | None:
    return _CURRENT_REQUEST_CONTEXT.get()


def current_request_session_key() -> str | None:
    ctx = current_request_context()
    return ctx.session_key if ctx else None


@dataclass
class ToolContext:
    config: Any = None
    workspace: str = ""
    bus: Any | None = None
    subagent_manager: Any | None = None
    sessions: Any | None = None


# ---- ContextBuilder (from step17b) ----


_DEFAULT_IDENTITY = "You are nanobot, a lightweight AI agent assistant."


@dataclass
class ContextBuilder:
    workspace: str = "."
    bootstrap_files: list[str] = field(
        default_factory=lambda: ["AGENTS.md", "SOUL.md", "USER.md"]
    )

    def build_system_prompt(
        self, identity: str | None = None, session_summary: str | None = None
    ) -> str:
        parts: list[str] = []
        parts.append(identity if identity else _DEFAULT_IDENTITY)

        ws = Path(self.workspace)
        for filename in self.bootstrap_files:
            file_path = ws / filename
            if file_path.is_file():
                content = file_path.read_text(encoding="utf-8")
                parts.append(f"## {filename}\n\n{content}")

        if session_summary:
            parts.append(f"[Archived Context Summary]\n\n{session_summary}")

        return "\n\n---\n\n".join(parts)

    def build_messages(
        self,
        current_message: str,
        history: list[dict[str, Any]] | None = None,
        identity: str | None = None,
        session_summary: str | None = None,
    ) -> list[dict[str, Any]]:
        system_content = self.build_system_prompt(identity=identity, session_summary=session_summary)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_content},
        ]
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": current_message})
        return messages
