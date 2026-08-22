"""step32 公共历史与运行时上下文展示期移除测试（A12 下半场）。

全构造数据：无真实 API。
"""

from __future__ import annotations

import pytest

from step101.context import ContextBuilder
from step101.runtime_context import (
    RUNTIME_CONTEXT_HISTORY_META,
    RuntimeContextBlock,
    append_runtime_context,
    public_history_message,
    public_history_messages,
)
from step101.session import Session, SessionManager
from step101.session.history_visibility import HIDDEN_HISTORY_META


# ---------------------------------------------------------------------------
# public_history_message：文本 / 多模态两种形态的精确移除
# ---------------------------------------------------------------------------


class TestPublicHistoryMessage:
    def test_no_marker_passthrough(self):
        msg = {"role": "user", "content": "hello"}
        cleaned = public_history_message(msg)
        assert cleaned == {"role": "user", "content": "hello"}
        # 深拷贝：原消息不受影响
        assert msg == {"role": "user", "content": "hello"}

    def test_wrong_marker_version_passthrough(self):
        msg = {
            "role": "user",
            "content": "hello\n\nextra",
            RUNTIME_CONTEXT_HISTORY_META: {"version": 99, "suffix": "extra"},
        }
        cleaned = public_history_message(msg)
        assert cleaned["content"] == "hello\n\nextra"
        assert RUNTIME_CONTEXT_HISTORY_META not in cleaned

    def test_text_suffix_exact_remove(self):
        content, marker = append_runtime_context(
            "hello", [RuntimeContextBlock(source="c", content="now=2026")],
        )
        msg = {"role": "user", "content": content, RUNTIME_CONTEXT_HISTORY_META: marker}
        cleaned = public_history_message(msg)
        assert cleaned["content"] == "hello"
        assert RUNTIME_CONTEXT_HISTORY_META not in cleaned

    def test_text_suffix_only_content(self):
        # content 完全等于 suffix（空原始内容 + 运行时上下文）
        content, marker = append_runtime_context(
            "", [RuntimeContextBlock(source="c", content="only")],
        )
        msg = {"role": "user", "content": content, RUNTIME_CONTEXT_HISTORY_META: marker}
        cleaned = public_history_message(msg)
        assert cleaned["content"] == ""

    def test_text_suffix_not_matching_passthrough(self):
        # marker 声称的 suffix 与实际 content 不匹配 → 不移除
        msg = {
            "role": "user",
            "content": "hello world",
            RUNTIME_CONTEXT_HISTORY_META: {"version": 1, "suffix": "different"},
        }
        cleaned = public_history_message(msg)
        assert cleaned["content"] == "hello world"

    def test_multimodal_blocks_exact_remove(self):
        content, marker = append_runtime_context(
            [{"type": "image_url", "image_url": {"url": "data:x"}}],
            [RuntimeContextBlock(source="c", content="extra")],
        )
        msg = {"role": "user", "content": content, RUNTIME_CONTEXT_HISTORY_META: marker}
        cleaned = public_history_message(msg)
        assert cleaned["content"] == [{"type": "image_url", "image_url": {"url": "data:x"}}]
        assert RUNTIME_CONTEXT_HISTORY_META not in cleaned

    def test_multimodal_blocks_not_matching_passthrough(self):
        msg = {
            "role": "user",
            "content": [{"type": "text", "text": "real"}],
            RUNTIME_CONTEXT_HISTORY_META: {
                "version": 1,
                "blocks": [{"type": "text", "text": "different"}],
            },
        }
        cleaned = public_history_message(msg)
        assert cleaned["content"] == [{"type": "text", "text": "real"}]

    def test_public_history_messages_batch(self):
        msgs = [
            {"role": "user", "content": "plain"},
            {
                "role": "user",
                "content": "hi\n\nctx",
                RUNTIME_CONTEXT_HISTORY_META: {"version": 1, "suffix": "ctx"},
            },
        ]
        cleaned = public_history_messages(msgs)
        assert cleaned[0]["content"] == "plain"
        assert cleaned[1]["content"] == "hi"
        assert RUNTIME_CONTEXT_HISTORY_META not in cleaned[1]


# ---------------------------------------------------------------------------
# ContextBuilder：build_messages 持久化 marker 到尾部消息
# ---------------------------------------------------------------------------


class TestContextBuilderMarkerPersistence:
    def _builder(self, tmp_path):
        return ContextBuilder(workspace=str(tmp_path))

    def test_marker_attached_to_user_tail(self, tmp_path):
        builder = self._builder(tmp_path)
        messages = builder.build_messages(
            current_message="hi",
            runtime_context_blocks=[RuntimeContextBlock(source="clock", content="now=1")],
        )
        tail = messages[-1]
        assert tail["role"] == "user"
        assert tail["content"] == "hi\n\nnow=1"
        assert RUNTIME_CONTEXT_HISTORY_META in tail
        assert tail[RUNTIME_CONTEXT_HISTORY_META]["version"] == 1
        assert tail[RUNTIME_CONTEXT_HISTORY_META]["suffix"] == "now=1"

    def test_marker_attached_when_merged_into_tail(self, tmp_path):
        # 历史末尾已是 user（续跑场景）：合并后 marker 也应在合并消息上
        builder = self._builder(tmp_path)
        messages = builder.build_messages(
            current_message="more",
            history=[{"role": "user", "content": "prev"}],
            runtime_context_blocks=[RuntimeContextBlock(source="c", content="now=2")],
        )
        tail = messages[-1]
        assert tail["role"] == "user"
        assert tail["content"] == "prev\nmore\n\nnow=2"
        assert RUNTIME_CONTEXT_HISTORY_META in tail
        assert tail[RUNTIME_CONTEXT_HISTORY_META]["suffix"] == "now=2"

    def test_no_marker_without_blocks(self, tmp_path):
        builder = self._builder(tmp_path)
        messages = builder.build_messages(current_message="plain")
        tail = messages[-1]
        assert RUNTIME_CONTEXT_HISTORY_META not in tail

    def test_no_marker_for_assistant_role(self, tmp_path):
        builder = self._builder(tmp_path)
        messages = builder.build_messages(
            current_message="",
            current_role="assistant",
            runtime_context_blocks=[RuntimeContextBlock(source="c", content="now=1")],
        )
        tail = messages[-1]
        assert tail["role"] == "assistant"
        assert RUNTIME_CONTEXT_HISTORY_META not in tail

    def test_marker_roundtrip_via_public_history(self, tmp_path):
        """build_messages → public_history_message 能还原原始内容。"""
        builder = self._builder(tmp_path)
        messages = builder.build_messages(
            current_message="hello world",
            runtime_context_blocks=[RuntimeContextBlock(source="clock", content="now=2026")],
        )
        tail = messages[-1]
        cleaned = public_history_message(tail)
        assert cleaned["content"] == "hello world"
        assert RUNTIME_CONTEXT_HISTORY_META not in cleaned


# ---------------------------------------------------------------------------
# Session.get_public_history：过滤隐藏行 + 移除运行时上下文
# ---------------------------------------------------------------------------


class TestSessionGetPublicHistory:
    def test_filters_hidden_history_messages(self, tmp_path):
        mgr = SessionManager(workspace=str(tmp_path))
        session = mgr.get_or_create("test")
        session.add_message("user", "visible 1")
        session.add_message("assistant", "response 1")
        session.add_message("user", "hidden injection", **{HIDDEN_HISTORY_META: True})
        session.add_message("user", "visible 2")

        public = session.get_public_history()
        roles_contents = [(m["role"], m["content"]) for m in public]
        assert ("user", "visible 1") in roles_contents
        assert ("assistant", "response 1") in roles_contents
        assert ("user", "hidden injection") not in roles_contents
        assert ("user", "visible 2") in roles_contents

    def test_removes_runtime_context_from_history(self, tmp_path):
        mgr = SessionManager(workspace=str(tmp_path))
        session = mgr.get_or_create("test")
        # 模拟持久化了带 marker 的消息（未来 marker 持久化策略启用后）
        content, marker = append_runtime_context(
            "real question", [RuntimeContextBlock(source="c", content="ctx=1")],
        )
        session.add_message(
            "user", content, **{RUNTIME_CONTEXT_HISTORY_META: marker},
        )
        session.add_message("assistant", "answer")

        public = session.get_public_history()
        assert public[0]["content"] == "real question"
        assert RUNTIME_CONTEXT_HISTORY_META not in public[0]

    def test_plain_history_unaffected(self, tmp_path):
        mgr = SessionManager(workspace=str(tmp_path))
        session = mgr.get_or_create("test")
        session.add_message("user", "hello")
        session.add_message("assistant", "hi there")

        public = session.get_public_history()
        assert len(public) == 2
        assert public[0]["content"] == "hello"
        assert public[1]["content"] == "hi there"

    def test_returns_deep_copy_not_mutation(self, tmp_path):
        mgr = SessionManager(workspace=str(tmp_path))
        session = mgr.get_or_create("test")
        session.add_message("user", "original")

        public = session.get_public_history()
        public[0]["content"] = "mutated"
        assert session.messages[0]["content"] == "original"

    def test_respects_max_messages(self, tmp_path):
        mgr = SessionManager(workspace=str(tmp_path))
        session = mgr.get_or_create("test")
        for i in range(10):
            session.add_message("user", f"msg {i}")

        public = session.get_public_history(max_messages=3)
        assert len(public) == 3
        assert public[0]["content"] == "msg 7"
