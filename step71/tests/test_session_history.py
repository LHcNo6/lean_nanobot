"""step33 Session 历史回放增强测试（A15）。

覆盖：
- recent_message_start_index 函数
- get_history 增强：_command 过滤、空 assistant 过滤、字段白名单、
  extend_to_user、include_runtime_context、user turn 对齐、find_legal_message_start
- get_public_history 不变性

全构造数据：无真实 API。
"""

from __future__ import annotations

import pytest

from step71.helpers import find_legal_message_start, recent_message_start_index
from step71.runtime_context import (
    RUNTIME_CONTEXT_HISTORY_META,
    RuntimeContextBlock,
    append_runtime_context,
)
from step71.session import Session, SessionManager
from step71.session.history_visibility import HIDDEN_HISTORY_META


# ---------------------------------------------------------------------------
# recent_message_start_index
# ---------------------------------------------------------------------------


class TestRecentMessageStartIndex:
    def test_max_messages_zero_returns_len(self):
        messages = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
        assert recent_message_start_index(messages, 0) == 2

    def test_basic_tail_slice(self):
        messages = [{"role": "user", "content": f"m{i}"} for i in range(10)]
        assert recent_message_start_index(messages, 3) == 7

    def test_extend_to_user_with_user_in_window(self):
        messages = [
            {"role": "user", "content": "u0"},
            {"role": "assistant", "content": "a0"},
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
        ]
        # 窗口 [a0, u1, a1] 内有 user，不扩展
        assert recent_message_start_index(messages, 3, extend_to_user=True) == 1

    def test_extend_to_user_recover_previous_user(self):
        messages = [
            {"role": "user", "content": "u0"},
            {"role": "assistant", "content": "a0"},
            {"role": "assistant", "content": "a1"},
            {"role": "assistant", "content": "a2"},
        ]
        # 窗口 [a0, a1, a2] 内无 user，向前恢复 u0
        assert recent_message_start_index(messages, 3, extend_to_user=True) == 0

    def test_extend_to_user_with_channel_delivery(self):
        messages = [
            {"role": "assistant", "content": "delivery", "_channel_delivery": True},
            {"role": "user", "content": "u0"},
            {"role": "assistant", "content": "a0"},
            {"role": "assistant", "content": "a1"},
        ]
        # 窗口 [a0, a1] 内无 user，向前恢复 u0，前一个是 _channel_delivery，包含
        assert recent_message_start_index(messages, 2, extend_to_user=True) == 0

    def test_extend_to_user_no_user_found(self):
        messages = [{"role": "assistant", "content": f"a{i}"} for i in range(5)]
        assert recent_message_start_index(messages, 3, extend_to_user=True) == 2

    def test_messages_less_than_max(self):
        messages = [{"role": "user", "content": "a"}]
        assert recent_message_start_index(messages, 10, extend_to_user=True) == 0


# ---------------------------------------------------------------------------
# get_history 增强
# ---------------------------------------------------------------------------


def _make_session(messages: list[dict], last_consolidated: int = 0) -> Session:
    return Session(
        key="test:1",
        messages=messages,
        last_consolidated=last_consolidated,
    )


class TestGetHistoryEnhanced:
    def test_command_messages_filtered(self):
        session = _make_session([
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi", "_command": True},
            {"role": "user", "content": "world"},
        ])
        history = session.get_history()
        roles = [m["role"] for m in history]
        assert roles == ["user", "user"]  # _command 消息被过滤

    def test_empty_assistant_filtered(self):
        session = _make_session([
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": ""},
            {"role": "user", "content": "world"},
        ])
        history = session.get_history()
        assert len(history) == 2
        assert history[0]["content"] == "hello"
        assert history[1]["content"] == "world"

    def test_empty_assistant_with_tool_calls_kept(self):
        session = _make_session([
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "1", "type": "function"}]},
            {"role": "tool", "content": "result", "tool_call_id": "1"},
        ])
        history = session.get_history()
        assert len(history) == 3
        assert history[1]["tool_calls"] is not None

    def test_field_whitelist_only(self):
        session = _make_session([
            {
                "role": "user",
                "content": "hello",
                "_internal_field": "should_be_removed",
                "metadata": {"extra": "data"},
            },
        ])
        history = session.get_history()
        assert len(history) == 1
        assert "_internal_field" not in history[0]
        assert "metadata" not in history[0]
        assert history[0]["role"] == "user"
        assert history[0]["content"] == "hello"

    def test_extend_to_user_false_default(self):
        session = _make_session([
            {"role": "user", "content": "u0"},
            {"role": "assistant", "content": "a0"},
            {"role": "assistant", "content": "a1"},
            {"role": "assistant", "content": "a2"},
        ])
        # 默认 extend_to_user=False，只取尾部 2 条
        history = session.get_history(max_messages=2)
        assert len(history) == 2
        assert history[0]["content"] == "a1"

    def test_extend_to_user_true(self):
        session = _make_session([
            {"role": "user", "content": "u0"},
            {"role": "assistant", "content": "a0"},
            {"role": "assistant", "content": "a1"},
            {"role": "assistant", "content": "a2"},
        ])
        # extend_to_user=True，向前恢复 u0
        history = session.get_history(max_messages=2, extend_to_user=True)
        assert len(history) == 4  # 从 u0 开始全部保留
        assert history[0]["content"] == "u0"

    def test_include_runtime_context_false(self):
        content, marker = append_runtime_context(
            "hello", [RuntimeContextBlock(source="c", content="now=2026")],
        )
        session = _make_session([
            {"role": "user", "content": content, RUNTIME_CONTEXT_HISTORY_META: marker},
        ])
        history = session.get_history(include_runtime_context=False)
        assert len(history) == 1
        assert RUNTIME_CONTEXT_HISTORY_META not in history[0]
        assert "Runtime Context" not in history[0]["content"]

    def test_include_runtime_context_true_default(self):
        content, marker = append_runtime_context(
            "hello", [RuntimeContextBlock(source="c", content="now=2026")],
        )
        session = _make_session([
            {"role": "user", "content": content, RUNTIME_CONTEXT_HISTORY_META: marker},
        ])
        history = session.get_history()  # 默认 include_runtime_context=True
        assert len(history) == 1
        # 默认保留运行时上下文（content 中包含运行时上下文内容）
        assert "now=2026" in history[0]["content"]

    def test_max_tokens_user_turn_alignment(self):
        # 构造消息，使 token 预算截断后第一个是 assistant，应对齐到 user
        session = _make_session([
            {"role": "user", "content": "u0"},
            {"role": "assistant", "content": "a0"},
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
        ])
        # 很小的 token 预算，只保留最后 1-2 条
        history = session.get_history(max_tokens=10)
        if history:
            # 第一条应该是 user（user turn 对齐）
            assert history[0]["role"] == "user"

    def test_legal_message_start_drops_orphan_tool_results(self):
        session = _make_session([
            {"role": "tool", "content": "orphan", "tool_call_id": "nonexistent"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ])
        history = session.get_history()
        # 孤立的 tool 结果应该被丢弃
        assert history[0]["role"] == "user"

    def test_respects_last_consolidated(self):
        session = _make_session([
            {"role": "user", "content": "archived"},
            {"role": "assistant", "content": "archived_resp"},
            {"role": "user", "content": "recent"},
            {"role": "assistant", "content": "recent_resp"},
        ], last_consolidated=2)
        history = session.get_history()
        assert len(history) == 2
        assert history[0]["content"] == "recent"


# ---------------------------------------------------------------------------
# get_public_history 不变性
# ---------------------------------------------------------------------------


class TestGetPublicHistoryUnchanged:
    def test_public_history_filters_hidden(self):
        session = _make_session([
            {"role": "user", "content": "visible"},
            {"role": "assistant", "content": "hidden", HIDDEN_HISTORY_META: True},
            {"role": "user", "content": "also_visible"},
        ])
        history = session.get_public_history()
        assert len(history) == 2
        assert history[0]["content"] == "visible"
        assert history[1]["content"] == "also_visible"

    def test_public_history_removes_runtime_context(self):
        content, marker = append_runtime_context(
            "hello", [RuntimeContextBlock(source="c", content="now=2026")],
        )
        session = _make_session([
            {"role": "user", "content": content, RUNTIME_CONTEXT_HISTORY_META: marker},
        ])
        history = session.get_public_history()
        assert len(history) == 1
        assert RUNTIME_CONTEXT_HISTORY_META not in history[0]
        assert "Runtime Context" not in history[0]["content"]


# ---------------------------------------------------------------------------
# replay_max_messages_for_context
# ---------------------------------------------------------------------------


class TestReplayMaxMessagesForContext:
    def test_none_returns_file_max(self):
        from step71.session.manager import FILE_MAX_MESSAGES, replay_max_messages_for_context
        assert replay_max_messages_for_context(None) == FILE_MAX_MESSAGES

    def test_zero_returns_file_max(self):
        from step71.session.manager import FILE_MAX_MESSAGES, replay_max_messages_for_context
        assert replay_max_messages_for_context(0) == FILE_MAX_MESSAGES

    def test_large_context(self):
        from step71.session.manager import FILE_MAX_MESSAGES, replay_max_messages_for_context
        # 128000 // 100 = 1280，小于 FILE_MAX_MESSAGES(2000)
        assert replay_max_messages_for_context(128000) == 1280

    def test_small_context_uses_min(self):
        from step71.session.manager import MIN_REPLAY_MAX_MESSAGES, replay_max_messages_for_context
        # 1000 // 100 = 10，小于 MIN_REPLAY_MAX_MESSAGES(120)
        assert replay_max_messages_for_context(1000) == MIN_REPLAY_MAX_MESSAGES

    def test_huge_context_capped_at_file_max(self):
        from step71.session.manager import FILE_MAX_MESSAGES, replay_max_messages_for_context
        # 1000000 // 100 = 10000，超过 FILE_MAX_MESSAGES(2000)
        assert replay_max_messages_for_context(1000000) == FILE_MAX_MESSAGES
