from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, Sequence, runtime_checkable

from step67.runtime_context import (
    RUNTIME_CONTEXT_HISTORY_META,
    RuntimeContextBlock,
    append_runtime_context,
)


# ---- Tool Context (Step 18) ----

_CURRENT_REQUEST_CONTEXT: ContextVar["RequestContext | None"] = ContextVar(
    "tool_request_context",
    default=None,
)


@dataclass(frozen=True)
class RequestContext:
    """每次消息处理时注入给工具的请求快照（对齐 nanobot ``agent/tools/context.py``）。

    Attributes:
        channel: 来源通道名（如 "cli" / "websocket"）。
        chat_id: 会话/聊天标识。
        message_id: 触发消息的 ID（可为 None）。
        session_key: 逻辑会话键（可为 None，如 dream 轮次）。
        original_user_text: 用户原始文本（注入前）。
        runtime: 本次 turn 使用的 LLMRuntime（可为 None）。
        metadata: 入站消息 metadata 副本。
        sender_id: 发送者标识（如 "subagent"）。
        turn_id: 本次 turn 的唯一 ID。
        workspace: 本次 turn 的 workspace scope 项目根（可为 None）。
    """

    channel: str = ""
    chat_id: str = ""
    message_id: str | None = None
    session_key: str | None = None
    original_user_text: str | None = None
    runtime: Any | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    sender_id: str | None = None
    turn_id: str | None = None
    workspace: Path | None = None


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
    """工具装配上下文（step29：拿到真实 workspace / config / 权限意图）。

    Attributes:
        config: 装配时的 Config 对象（step25 起由 loop 透传，可空）。
        workspace: 项目根目录（str）；step29 起为 effective scope 的
            ``project_path``（受限/非受限均由该值决定工具默认根）。
        restrict_to_workspace: 权限意图 —— 工具是否应把文件访问限制在
            workspace 内（对齐 nanobot ``config.tools.restrict_to_workspace``）。
        bus: 消息总线（可空）。
        subagent_manager: 子代理管理器（可空）。
        sessions: 会话管理器（可空）。
        session_key: 当前会话键（可空）。
    """

    config: Any = None
    workspace: str = ""
    restrict_to_workspace: bool = False
    bus: Any | None = None
    subagent_manager: Any | None = None
    sessions: Any | None = None
    session_key: str | None = None
    file_state_store: Any = None  # step65：FileStateStore 实例，按 session_key 管理 FileStates


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
            from step67.skills import SkillsLoader

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
        workspace: Path | None = None,
        include_memory_recent_history: bool = True,
    ) -> str:
        """构建 system prompt（step29 起支持按 turn 覆盖引导文件根目录）。

        step41：新增 ``include_memory_recent_history`` 参数（对齐 nanobot）。
        ephemeral turn 传 False——临时 turn 不读取跨会话记忆。当前 step41 尚无
        memory 集成，该参数为接口对齐（no-op），等 memory 集成后填充实际逻辑。

        Args:
            identity: 覆盖默认人格文本。
            session_summary: 归档上下文摘要（附于尾部）。
            skill_names: 显式全量注入的技能名（对齐 step27 扩展）。
            workspace: 本 turn 生效的项目根；None 时用构造时的 ``self.workspace``。
                技能目录仍按 ``self.workspace`` 解析（取舍：技能随装配走）。
            include_memory_recent_history: 是否包含跨会话记忆（ephemeral 为 False）。
        """
        parts: list[str] = []
        parts.append(identity if identity else _DEFAULT_IDENTITY)

        ws = workspace or Path(self.workspace)
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
        runtime_context_blocks: Sequence[RuntimeContextBlock] | None = None,
        workspace: Path | None = None,
        include_memory_recent_history: bool = True,
    ) -> list[dict[str, Any]]:
        """构建一次 LLM 调用的完整消息列表。

        step41：新增 ``include_memory_recent_history`` 参数（对齐 nanobot），
        透传到 ``build_system_prompt``。ephemeral turn 传 False。当前为接口
        对齐（no-op），等 memory 集成后填充实际逻辑。

        Args:
            current_message: 当前用户内容（subagent 跟进时为 ""）。
            history: 会话历史消息（可为 None）。
            identity: 覆盖人格文本。
            session_summary: 摘要（传给 system prompt）。
            current_role: 尾部消息角色（"user" / "assistant"）。
            runtime_context_blocks: 运行时上下文块；仅 ``current_role=="user"``
                时追加到用户内容尾部（对齐 nanobot：assistant 角色跳过）。
            workspace: 本 turn 的项目根（用于 bootstrap 文件解析）。
            include_memory_recent_history: 是否包含跨会话记忆（ephemeral 为 False）。

        Returns:
            ``[system, *history, tail]`` 消息列表；尾部若与 ``current_role``
            重复则做内容合并而非追加（对齐 step23 的角色交替语义）。
        """
        blocks = list(runtime_context_blocks or ()) if current_role == "user" else []
        merged, rc_meta = append_runtime_context(current_message, blocks)
        system_content = self.build_system_prompt(
            identity=identity,
            session_summary=session_summary,
            workspace=workspace,
            include_memory_recent_history=include_memory_recent_history,
        )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_content},
        ]
        if history:
            messages.extend(history)
        if messages and messages[-1].get("role") == current_role:
            # Subagent follow-ups were already persisted as assistant messages;
            # merging (instead of appending) avoids empty-role duplicates.
            if merged:
                last = dict(messages[-1])
                last["content"] = (
                    str(last.get("content") or "") + "\n" + merged
                ).strip()
                if rc_meta:
                    last[RUNTIME_CONTEXT_HISTORY_META] = rc_meta
                messages[-1] = last
            return messages
        tail_msg: dict[str, Any] = {"role": current_role, "content": merged}
        if rc_meta:
            tail_msg[RUNTIME_CONTEXT_HISTORY_META] = rc_meta
        messages.append(tail_msg)
        return messages
