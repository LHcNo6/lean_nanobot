"""Tests for Step 7 — Session persistence with SessionManager."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any

from step7.context import ContextBuilder
from step7.llm import LLMResponse
from step7.runner import AgentRunSpec, AgentRunner
from step7.session import Session, SessionManager, safe_filename
from step7.tool import ToolRegistry
from step7.tools.echo import EchoTool


class _MockProvider:
    def __init__(self, response: LLMResponse) -> None:
        self._response = response

    async def chat_with_retry(self, **kwargs: Any) -> LLMResponse:
        return self._response


class TestSafeFilename(unittest.TestCase):
    def test_replaces_unsafe_chars(self):
        self.assertEqual(safe_filename("a:b/c"), "a_b_c")


class TestSession(unittest.TestCase):
    def test_add_message(self):
        session = Session(key="test")
        msg = session.add_message("user", "Hello")
        self.assertEqual(len(session.messages), 1)
        self.assertEqual(msg["role"], "user")
        self.assertEqual(msg["content"], "Hello")
        self.assertIn("timestamp", msg)

    def test_get_history(self):
        session = Session(key="test")
        session.add_message("user", "Hi")
        session.add_message("assistant", "Hello!")
        history = session.get_history()
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["content"], "Hi")
        self.assertEqual(history[1]["content"], "Hello!")

    def test_get_history_max_messages(self):
        session = Session(key="test")
        for i in range(10):
            session.add_message("user", str(i))
        history = session.get_history(max_messages=3)
        self.assertEqual(len(history), 3)
        self.assertEqual(history[0]["content"], "7")
        self.assertEqual(history[-1]["content"], "9")

    def test_get_history_with_last_consolidated(self):
        session = Session(key="test")
        for i in range(5):
            session.add_message("user", str(i))
        session.last_consolidated = 3
        history = session.get_history()
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["content"], "3")
        self.assertEqual(history[1]["content"], "4")

    def test_import_messages_adds_timestamp(self):
        session = Session(key="test")
        raw = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
        session.import_messages(raw)
        self.assertEqual(len(session.messages), 2)
        for msg in session.messages:
            self.assertIn("timestamp", msg)

    def test_import_messages_preserves_existing_timestamp(self):
        session = Session(key="test")
        raw = [{"role": "user", "content": "hi", "timestamp": "2024-01-01T00:00:00"}]
        session.import_messages(raw)
        self.assertEqual(session.messages[0]["timestamp"], "2024-01-01T00:00:00")

    def test_import_messages_updates_updated_at(self):
        session = Session(key="test")
        session.updated_at = "2000-01-01T00:00:00"
        session.import_messages([{"role": "user", "content": "hi"}])
        self.assertGreater(session.updated_at, "2000-01-01T00:00:00")

    def test_import_messages_does_not_mutate_input(self):
        session = Session(key="test")
        raw = [{"role": "user", "content": "hi"}]
        session.import_messages(raw)
        self.assertNotIn("timestamp", raw[0])


class TestSessionManager(unittest.TestCase):
    def test_get_or_create_new(self):
        with tempfile.TemporaryDirectory() as tmp:
            sm = SessionManager(workspace=tmp)
            session = sm.get_or_create("new_user")
            self.assertEqual(session.key, "new_user")
            self.assertEqual(session.messages, [])

    def test_save_and_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            sm = SessionManager(workspace=tmp)
            session = sm.get_or_create("alice")
            session.add_message("user", "Hi")
            session.add_message("assistant", "Hello")
            sm.save(session)

            sm2 = SessionManager(workspace=tmp)
            loaded = sm2.get_or_create("alice")
            self.assertEqual(len(loaded.messages), 2)
            self.assertEqual(loaded.messages[0]["content"], "Hi")
            self.assertEqual(loaded.messages[1]["content"], "Hello")

    def test_save_appends(self):
        with tempfile.TemporaryDirectory() as tmp:
            sm = SessionManager(workspace=tmp)
            session = sm.get_or_create("bob")
            session.add_message("user", "first")
            sm.save(session)

            session.add_message("assistant", "reply")
            sm.save(session)

            sm2 = SessionManager(workspace=tmp)
            loaded = sm2.get_or_create("bob")
            self.assertEqual(len(loaded.messages), 2)

    def test_cache_hit(self):
        with tempfile.TemporaryDirectory() as tmp:
            sm = SessionManager(workspace=tmp)
            s1 = sm.get_or_create("cache_test")
            s2 = sm.get_or_create("cache_test")
            self.assertIs(s1, s2)

    def test_corrupt_file_returns_new_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sessions" / f"{safe_filename('corrupt')}.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("not json\n", encoding="utf-8")

            sm = SessionManager(workspace=tmp)
            session = sm.get_or_create("corrupt")
            self.assertEqual(session.messages, [])

    def test_safe_filename_in_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            sm = SessionManager(workspace=tmp)
            session = sm.get_or_create("user:123")
            session.add_message("user", "hi")
            sm.save(session)
            expected = sm.sessions_dir / "user_123.jsonl"
            self.assertTrue(expected.exists())

    def test_tmp_file_cleaned_on_save_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            sm = SessionManager(workspace=tmp)
            session = sm.get_or_create("cleanup")
            path = sm._session_path("cleanup")
            tmp_path = path.with_suffix(".jsonl.tmp")
            session.add_message("user", "hi")
            sm.save(session)
            self.assertFalse(tmp_path.exists())

    def test_fsync_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            sm = SessionManager(workspace=tmp)
            session = sm.get_or_create("fsync_test")
            session.add_message("user", "hi")
            sm.save(session, fsync=True)
            path = sm._session_path("fsync_test")
            self.assertTrue(path.exists())

    def test_last_consolidated_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            sm = SessionManager(workspace=tmp)
            session = sm.get_or_create("consolidated")
            session.add_message("user", "a")
            session.add_message("user", "b")
            session.last_consolidated = 1
            sm.save(session)

            sm2 = SessionManager(workspace=tmp)
            loaded = sm2.get_or_create("consolidated")
            self.assertEqual(loaded.last_consolidated, 1)
            self.assertEqual(len(loaded.messages), 2)


class TestIntegrationMultiTurn(unittest.IsolatedAsyncioTestCase):
    async def test_multi_turn_flow(self):
        """Full flow: Session → ContextBuilder → AgentRunner → Save → Next turn."""
        registry = ToolRegistry()
        registry.register(EchoTool())

        provider = _MockProvider(
            LLMResponse(content="First reply", finish_reason="stop")
        )

        with tempfile.TemporaryDirectory() as tmp:
            sm = SessionManager(workspace=tmp)
            session = sm.get_or_create("multi_turn")

            # Turn 1
            history = session.get_history(max_messages=20)
            ctx = ContextBuilder(workspace=tmp)
            msgs = ctx.build_messages("Hello", history=history)
            spec = AgentRunSpec(initial_messages=msgs, tools=registry, provider=provider)
            result = await AgentRunner().run(spec)
            skip = 1 + len(history)
            session.import_messages(result.messages[skip:])
            sm.save(session)
            self.assertEqual(len(session.messages), 2)  # user + assistant
            self.assertEqual(session.messages[0]["content"], "Hello")
            self.assertEqual(session.messages[1]["content"], "First reply")

            # Turn 2 — provider returns different response
            provider._response = LLMResponse(content="Second reply", finish_reason="stop")
            history = session.get_history(max_messages=20)
            msgs = ctx.build_messages("Again", history=history)
            spec = AgentRunSpec(initial_messages=msgs, tools=registry, provider=provider)
            result = await AgentRunner().run(spec)
            skip = 1 + len(history)
            session.import_messages(result.messages[skip:])
            sm.save(session)
            self.assertEqual(len(session.messages), 4)
            self.assertEqual(session.messages[2]["content"], "Again")
            self.assertEqual(session.messages[3]["content"], "Second reply")

            # Reload from disk — verify persistence
            sm2 = SessionManager(workspace=tmp)
            reloaded = sm2.get_or_create("multi_turn")
            self.assertEqual(len(reloaded.messages), 4)
            self.assertEqual(reloaded.messages[0]["content"], "Hello")
            self.assertEqual(reloaded.messages[2]["content"], "Again")

    async def test_multi_turn_with_tool_calls(self):
        """Tool calls and results are persisted in session."""
        registry = ToolRegistry()
        registry.register(EchoTool())

        tc = type("ToolCall", (), {"id": "call_1", "name": "echo", "arguments": {"text": "hi"}})()

        with tempfile.TemporaryDirectory() as tmp:
            sm = SessionManager(workspace=tmp)
            session = sm.get_or_create("tool_session")
            history = session.get_history()
            ctx = ContextBuilder(workspace=tmp)
            msgs = ctx.build_messages("Echo hi", history=history)
            spec = AgentRunSpec(initial_messages=msgs, tools=registry, provider=_MockProvider(
                LLMResponse(content="Done", tool_calls=[tc], finish_reason="tool_calls")
            ))
            result = await AgentRunner().run(spec)

            skip = 1 + len(history)
            session.import_messages(result.messages[skip:])
            sm.save(session)

            # user, assistant(tool_calls), tool result, assistant(final)
            self.assertGreaterEqual(len(session.messages), 4)
            tool_roles = [m["role"] for m in session.messages]
            self.assertIn("tool", tool_roles)

            sm2 = SessionManager(workspace=tmp)
            loaded = sm2.get_or_create("tool_session")
            self.assertEqual(len(loaded.messages), len(session.messages))
            self.assertIn("tool", [m["role"] for m in loaded.messages])


if __name__ == "__main__":
    unittest.main()
