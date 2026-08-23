"""step34 测试：_persist_user_message_early 提前持久化 + _build_initial_messages 提取。

全构造数据：假 provider + tmp_path；无真实 API。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from step77.bus import MessageBus
from step77.bus.events import InboundMessage
from step77.context import ContextBuilder
from step77.loop import AgentLoop
from step77.memory import MemoryStore
from step77.runtime_context import (
    RUNTIME_CONTEXT_HISTORY_META,
    RuntimeContextBlock,
)
from step77.session import Session, SessionManager
from step77.goal_state import GOAL_STATE_KEY
from step77.tool import ToolRegistry


def _mk_loop(tmp_path: Path, **kwargs: Any) -> AgentLoop:
    """构造最小 AgentLoop（provider 用 None 即可，run 前不触碰）。"""
    bus = MessageBus()
    return AgentLoop(
        bus=bus,
        provider=None,
        registry=ToolRegistry(),
        session_manager=SessionManager(workspace=str(tmp_path)),
        context_builder=ContextBuilder(workspace=str(tmp_path)),
        memory=MemoryStore(workspace=str(tmp_path)),
        identity="You are a test bot.",
        replay_budget=10_000,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# _persist_user_message_early
# ---------------------------------------------------------------------------


class TestPersistUserEarly:
    """_persist_user_message_early 方法测试。"""

    def test_persists_with_runtime_context(self, tmp_path: Path) -> None:
        """含运行时上下文时，持久化消息包含运行时上下文 + marker。"""
        loop = _mk_loop(tmp_path)
        msg = InboundMessage(content="hello", chat_id="chat1", sender_id="user")
        session = Session(key="chat1")
        blocks = [RuntimeContextBlock(source="clock", content="now=2026")]

        result = loop._persist_user_message_early(msg, session, runtime_context_blocks=blocks)

        assert result is True
        assert len(session.messages) == 1
        persisted = session.messages[-1]
        assert persisted["role"] == "user"
        assert "hello" in persisted["content"]
        assert "now=2026" in persisted["content"]
        assert RUNTIME_CONTEXT_HISTORY_META in persisted
        assert persisted[RUNTIME_CONTEXT_HISTORY_META]["sources"] == ["clock"]

    def test_persists_without_runtime_context(self, tmp_path: Path) -> None:
        """无运行时上下文时，持久化原始文本，无 marker。"""
        loop = _mk_loop(tmp_path)
        msg = InboundMessage(content="hello", chat_id="chat1", sender_id="user")
        session = Session(key="chat1")

        result = loop._persist_user_message_early(msg, session)

        assert result is True
        assert len(session.messages) == 1
        persisted = session.messages[-1]
        assert persisted["role"] == "user"
        assert persisted["content"] == "hello"
        assert RUNTIME_CONTEXT_HISTORY_META not in persisted

    def test_skip_user_persist_metadata_returns_false(self, tmp_path: Path) -> None:
        """_skip_user_persist=True 时不持久化。"""
        loop = _mk_loop(tmp_path)
        msg = InboundMessage(
            content="hello", chat_id="chat1", sender_id="user",
            metadata={"_skip_user_persist": True},
        )
        session = Session(key="chat1")

        result = loop._persist_user_message_early(msg, session)

        assert result is False
        assert len(session.messages) == 0

    def test_internal_continuation_not_persisted(self, tmp_path: Path) -> None:
        """内部续跑消息不持久化。"""
        loop = _mk_loop(tmp_path)
        from step77.session.turn_continuation import INTERNAL_CONTINUATION_META
        msg = InboundMessage(
            content="Continue the active goal...", chat_id="chat1", sender_id="user",
            metadata={INTERNAL_CONTINUATION_META: True},
        )
        session = Session(key="chat1")

        result = loop._persist_user_message_early(msg, session)

        assert result is False
        assert len(session.messages) == 0

    def test_empty_content_without_blocks_not_persisted(self, tmp_path: Path) -> None:
        """空文本且无运行时上下文时不持久化。"""
        loop = _mk_loop(tmp_path)
        msg = InboundMessage(content="   ", chat_id="chat1", sender_id="user")
        session = Session(key="chat1")

        result = loop._persist_user_message_early(msg, session)

        assert result is False
        assert len(session.messages) == 0

    def test_empty_content_with_blocks_persisted(self, tmp_path: Path) -> None:
        """空文本但有运行时上下文时仍持久化（运行时上下文本身有价值）。"""
        loop = _mk_loop(tmp_path)
        msg = InboundMessage(content="", chat_id="chat1", sender_id="user")
        session = Session(key="chat1")
        blocks = [RuntimeContextBlock(source="clock", content="now=2026")]

        result = loop._persist_user_message_early(msg, session, runtime_context_blocks=blocks)

        assert result is True
        assert len(session.messages) == 1
        assert "now=2026" in session.messages[-1]["content"]

    def test_marks_pending_user_turn(self, tmp_path: Path) -> None:
        """持久化后设置 pending_user_turn 标记。"""
        loop = _mk_loop(tmp_path)
        msg = InboundMessage(content="hello", chat_id="chat1", sender_id="user")
        session = Session(key="chat1")

        loop._persist_user_message_early(msg, session)

        assert session.metadata.get("pending_user_turn") is True

    def test_saves_session(self, tmp_path: Path) -> None:
        """持久化后调用 sessions.save（验证 session_manager.save 被调用）。"""
        loop = _mk_loop(tmp_path)
        saved = []

        def _fake_save(sess: Session) -> None:
            saved.append(sess.key)

        loop.sessions.save = _fake_save  # type: ignore[method-assign]
        msg = InboundMessage(content="hello", chat_id="chat1", sender_id="user")
        session = Session(key="chat1")

        loop._persist_user_message_early(msg, session)

        assert "chat1" in saved

    def test_extra_kwargs_persisted(self, tmp_path: Path) -> None:
        """**kwargs 额外元数据会被持久化到消息中。"""
        loop = _mk_loop(tmp_path)
        msg = InboundMessage(content="hello", chat_id="chat1", sender_id="user")
        session = Session(key="chat1")

        result = loop._persist_user_message_early(msg, session, custom_field="value123")

        assert result is True
        assert session.messages[-1].get("custom_field") == "value123"


# ---------------------------------------------------------------------------
# _build_initial_messages
# ---------------------------------------------------------------------------


class TestBuildInitialMessages:
    """_build_initial_messages 方法测试。"""

    def test_builds_with_user_role(self, tmp_path: Path) -> None:
        """current_role=user 时，尾部消息为 user 角色，内容为 msg.content。"""
        loop = _mk_loop(tmp_path)
        msg = InboundMessage(content="hello", chat_id="chat1", sender_id="user")
        session = Session(key="chat1")

        messages = loop._build_initial_messages(msg, session, history=[], pending_summary=None)

        assert messages[0]["role"] == "system"
        assert messages[-1]["role"] == "user"
        assert messages[-1]["content"] == "hello"

    def test_builds_with_assistant_role(self, tmp_path: Path) -> None:
        """current_role=assistant 时，current_message 为空（subagent follow-up）。"""
        loop = _mk_loop(tmp_path)
        msg = InboundMessage(content="subagent result", chat_id="chat1", sender_id="subagent")
        session = Session(key="chat1")

        messages = loop._build_initial_messages(
            msg, session, history=[], pending_summary=None, current_role="assistant",
        )

        assert messages[-1]["role"] == "assistant"
        # assistant 角色时 current_message 为空字符串
        assert messages[-1]["content"] == ""

    def test_includes_runtime_context_blocks(self, tmp_path: Path) -> None:
        """运行时上下文块附加到用户消息尾部。"""
        loop = _mk_loop(tmp_path)
        msg = InboundMessage(content="hello", chat_id="chat1", sender_id="user")
        session = Session(key="chat1")
        blocks = [RuntimeContextBlock(source="clock", content="now=2026")]

        messages = loop._build_initial_messages(
            msg, session, history=[], pending_summary=None,
            runtime_context_blocks=blocks,
        )

        assert "hello" in messages[-1]["content"]
        assert "now=2026" in messages[-1]["content"]
        assert RUNTIME_CONTEXT_HISTORY_META in messages[-1]

    def test_includes_goal_state_lines(self, tmp_path: Path) -> None:
        """goal state 运行时行合并到 identity（system prompt 中）。"""
        loop = _mk_loop(tmp_path)
        msg = InboundMessage(content="hello", chat_id="chat1", sender_id="user")
        session = Session(key="chat1")
        session.metadata[GOAL_STATE_KEY] = {
            "status": "active",
            "objective": "write a report",
        }

        messages = loop._build_initial_messages(msg, session, history=[], pending_summary=None)

        system_content = messages[0]["content"]
        assert "write a report" in system_content

    def test_includes_history(self, tmp_path: Path) -> None:
        """历史消息包含在 initial_messages 中（system 之后，当前消息之前）。"""
        loop = _mk_loop(tmp_path)
        msg = InboundMessage(content="second", chat_id="chat1", sender_id="user")
        session = Session(key="chat1")
        history = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "reply to first"},
        ]

        messages = loop._build_initial_messages(msg, session, history=history, pending_summary=None)

        assert len(messages) == 4  # system + 2 history + current
        assert messages[1]["content"] == "first"
        assert messages[2]["content"] == "reply to first"
        assert messages[3]["content"] == "second"

    def test_includes_pending_summary(self, tmp_path: Path) -> None:
        """pending_summary 传入 system prompt。"""
        loop = _mk_loop(tmp_path)
        msg = InboundMessage(content="hello", chat_id="chat1", sender_id="user")
        session = Session(key="chat1")

        messages = loop._build_initial_messages(
            msg, session, history=[], pending_summary="Previous conversation summary",
        )

        system_content = messages[0]["content"]
        assert "Previous conversation summary" in system_content
