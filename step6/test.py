"""Tests for Step 6 — ContextBuilder system prompt assembly."""

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from step6.context import ContextBuilder
from step6.llm import LLMResponse
from step6.runner import AgentRunSpec, AgentRunner
from step6.tool import ToolRegistry
from step6.tools.echo import EchoTool


class _MockProvider:
    def __init__(self, response: LLMResponse) -> None:
        self._response = response

    async def chat_with_retry(self, **kwargs: Any) -> LLMResponse:
        return self._response


class TestBuildSystemPrompt(unittest.TestCase):
    def test_default_identity_no_bootstrap(self):
        """No bootstrap files exist — identity is the default string."""
        with tempfile.TemporaryDirectory() as tmp:
            ctx = ContextBuilder(workspace=tmp)
            prompt = ctx.build_system_prompt()
        self.assertIn("You are nanobot", prompt)
        self.assertNotIn("## AGENTS.md", prompt)

    def test_with_agents_md(self):
        """AGENTS.md exists — it appears in the prompt."""
        content = "Be concise."
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "AGENTS.md").write_text(content, encoding="utf-8")
            ctx = ContextBuilder(workspace=tmp)
            prompt = ctx.build_system_prompt()
        self.assertIn("## AGENTS.md", prompt)
        self.assertIn(content, prompt)

    def test_all_three_bootstrap(self):
        """All three bootstrap files appear in order."""
        files = {
            "AGENTS.md": "Agent rules.",
            "SOUL.md": "Soul definition.",
            "USER.md": "User preferences.",
        }
        with tempfile.TemporaryDirectory() as tmp:
            for name, content in files.items():
                Path(tmp, name).write_text(content, encoding="utf-8")
            ctx = ContextBuilder(workspace=tmp)
            prompt = ctx.build_system_prompt()
        for name in files:
            self.assertIn(f"## {name}", prompt)
        self.assertIn("Agent rules.", prompt)
        self.assertIn("Soul definition.", prompt)
        self.assertIn("User preferences.", prompt)

    def test_custom_identity(self):
        """Custom identity overrides default."""
        with tempfile.TemporaryDirectory() as tmp:
            ctx = ContextBuilder(workspace=tmp)
            prompt = ctx.build_system_prompt(identity="You are FooBot.")
        self.assertEqual(prompt, "You are FooBot.")

    def test_nonexistent_bootstrap_ignored(self):
        """Missing bootstrap files cause no error and no section."""
        ctx = ContextBuilder(workspace="nonexistent_dir_12345")
        prompt = ctx.build_system_prompt()
        self.assertIn("You are nanobot", prompt)
        self.assertNotIn("## AGENTS.md", prompt)
        self.assertNotIn("## SOUL.md", prompt)
        self.assertNotIn("## USER.md", prompt)

    def test_custom_bootstrap_list(self):
        """Custom bootstrap file list is respected."""
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "CUSTOM.md").write_text("custom content", encoding="utf-8")
            ctx = ContextBuilder(workspace=tmp, bootstrap_files=["CUSTOM.md"])
            prompt = ctx.build_system_prompt()
        self.assertIn("## CUSTOM.md", prompt)
        self.assertIn("custom content", prompt)


class TestBuildMessages(unittest.TestCase):
    def test_no_history(self):
        """build_messages with only current_message."""
        with tempfile.TemporaryDirectory() as tmp:
            ctx = ContextBuilder(workspace=tmp)
            msgs = ctx.build_messages("Hello")
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0]["role"], "system")
        self.assertIn("You are nanobot", msgs[0]["content"])
        self.assertEqual(msgs[1], {"role": "user", "content": "Hello"})

    def test_with_history(self):
        """build_messages preserves history order."""
        history = [
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello!"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            ctx = ContextBuilder(workspace=tmp)
            msgs = ctx.build_messages("How are you?", history=history)
        self.assertEqual(len(msgs), 4)
        self.assertEqual(msgs[0]["role"], "system")
        self.assertEqual(msgs[1], history[0])
        self.assertEqual(msgs[2], history[1])
        self.assertEqual(msgs[3]["role"], "user")
        self.assertEqual(msgs[3]["content"], "How are you?")

    def test_identity_override_in_build_messages(self):
        """build_messages passes identity through to build_system_prompt."""
        with tempfile.TemporaryDirectory() as tmp:
            ctx = ContextBuilder(workspace=tmp)
            msgs = ctx.build_messages("Hi", identity="You are OverrideBot.")
        self.assertIn("OverrideBot.", msgs[0]["content"])

    def test_bootstrap_in_build_messages(self):
        """Bootstrap files appear in system content of build_messages."""
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "AGENTS.md").write_text("Rule 1", encoding="utf-8")
            ctx = ContextBuilder(workspace=tmp)
            msgs = ctx.build_messages("Hi")
        self.assertIn("## AGENTS.md", msgs[0]["content"])
        self.assertIn("Rule 1", msgs[0]["content"])


class TestIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_integration_with_runner(self):
        """ContextBuilder output passes through AgentRunner successfully."""
        registry = ToolRegistry()
        registry.register(EchoTool())

        provider = _MockProvider(
            LLMResponse(content="Got it", finish_reason="stop")
        )

        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "AGENTS.md").write_text("Be helpful.", encoding="utf-8")
            ctx = ContextBuilder(workspace=tmp)
            msgs = ctx.build_messages("Hello")

        spec = AgentRunSpec(
            initial_messages=msgs,
            tools=registry,
            provider=provider,
        )
        result = await AgentRunner().run(spec)
        self.assertEqual(result.final_content, "Got it")
        self.assertEqual(result.stop_reason, "stop")
        self.assertIn("## AGENTS.md", result.messages[0]["content"])


if __name__ == "__main__":
    unittest.main()
