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
    session_key: str | None = None


# ---- ContextBuilder (from step17b) ----


_DEFAULT_IDENTITY = "You are nanobot, a lightweight AI agent assistant."


@dataclass
class ContextBuilder:
    workspace: str = "."
    bootstrap_files: list[str] = field(
        default_factory=lambda: ["AGENTS.md", "SOUL.md", "USER.md"]
    )
    disabled_skills: list[str] = field(default_factory=list)
    builtin_skills_dir: str | None = None
    _skills: Any = field(default=None, init=False, repr=False)

    @property
    def skills(self) -> Any:
        """惰性构建（并缓存）SkillsLoader，供技能注入与使用。"""
        if self._skills is None:
            from step27.skills import SkillsLoader

            self._skills = SkillsLoader(
                Path(self.workspace),
                builtin_skills_dir=Path(self.builtin_skills_dir) if self.builtin_skills_dir else None,
                disabled_skills=set(self.disabled_skills),
            )
        return self._skills

    def build_system_prompt(
        self,
        identity: str | None = None,
        session_summary: str | None = None,
        skill_names: list[str] | None = None,
    ) -> str:
        parts: list[str] = []
        parts.append(identity if identity else _DEFAULT_IDENTITY)

        ws = Path(self.workspace)
        for filename in self.bootstrap_files:
            file_path = ws / filename
            if file_path.is_file():
                content = file_path.read_text(encoding="utf-8")
                parts.append(f"## {filename}\n\n{content}")

        # Step 27：技能注入（对齐 nanobot ContextBuilder.build_system_prompt）。
        # 1) always 技能全量注入（# Active Skills）；
        # 2) 其余技能只给渐进加载摘要（# Skills，agent 需用时 read_file 读全文）；
        # 3) skill_names 显式指定的技能也全量注入（lean 扩展：nanobot 声明未用）。
        always_skills = list(self.skills.get_always_skills())
        explicit = [n for n in (skill_names or []) if n not in always_skills]
        full_content = always_skills + explicit
        if full_content:
            content = self.skills.load_skills_for_context(full_content)
            if content:
                parts.append(f"# Active Skills\n\n{content}")

        skills_summary = self.skills.build_skills_summary(
            exclude=set(always_skills + explicit)
        )
        if skills_summary:
            parts.append(
                "# Skills\n\n"
                "The following skills extend your capabilities. To use a skill, "
                "read its SKILL.md file using the read_file tool. "
                "Unavailable skills need dependencies installed first.\n\n"
                + skills_summary
            )

        if session_summary:
            parts.append(f"[Archived Context Summary]\n\n{session_summary}")

        return "\n\n---\n\n".join(parts)

    def build_messages(
        self,
        current_message: str,
        history: list[dict[str, Any]] | None = None,
        identity: str | None = None,
        session_summary: str | None = None,
        current_role: str = "user",
    ) -> list[dict[str, Any]]:
        system_content = self.build_system_prompt(identity=identity, session_summary=session_summary)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_content},
        ]
        if history:
            messages.extend(history)
        if messages and messages[-1].get("role") == current_role:
            # Subagent follow-ups were already persisted as assistant messages;
            # merging (instead of appending) avoids empty-role duplicates.
            if current_message:
                last = dict(messages[-1])
                last["content"] = (
                    str(last.get("content") or "") + "\n" + current_message
                ).strip()
                messages[-1] = last
            return messages
        messages.append({"role": current_role, "content": current_message})
        return messages
