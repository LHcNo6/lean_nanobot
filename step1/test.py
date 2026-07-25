"""Tests for Step 1 — Provider abstraction."""

import json
import os
import unittest
from unittest import mock

from step1.llm import LLMProvider, LLMResponse, ToolCallRequest, OpenAICompatProvider


class TestDataTypes(unittest.TestCase):
    def test_tool_call_request(self):
        tc = ToolCallRequest(id="call_1", name="echo", arguments={"text": "hello"})
        self.assertEqual(tc.name, "echo")

    def test_llm_response(self):
        resp = LLMResponse(content="Hello", finish_reason="stop", usage={"prompt_tokens": 5})
        self.assertEqual(resp.content, "Hello")
        self.assertFalse(resp.tool_calls)

    def test_llm_response_with_tools(self):
        tc = ToolCallRequest(id="call_1", name="echo", arguments={"text": "hi"})
        resp = LLMResponse(content=None, tool_calls=[tc], finish_reason="tool_calls")
        self.assertEqual(len(resp.tool_calls), 1)


class TestLLMProvider(unittest.TestCase):
    def test_abc_cannot_instantiate(self):
        with self.assertRaises(TypeError):
            LLMProvider()  # type: ignore


class TestOpenAICompatProvider(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        os.environ["OPENAI_API_KEY"] = "sk-test"

    def tearDown(self):
        os.environ.pop("OPENAI_API_KEY", None)

    @mock.patch("step1.llm.AsyncOpenAI")
    async def test_chat_basic(self, mock_sdk):
        fake_msg = mock.MagicMock()
        fake_msg.content = "Hello!"
        fake_msg.tool_calls = None

        fake_choice = mock.MagicMock()
        fake_choice.finish_reason = "stop"
        fake_choice.message = fake_msg

        fake_usage = mock.MagicMock()
        fake_usage.prompt_tokens = 10
        fake_usage.completion_tokens = 5

        fake_resp = mock.MagicMock()
        fake_resp.choices = [fake_choice]
        fake_resp.usage = fake_usage
        fake_resp.model = "gpt-4o-mini"
        fake_resp.id = "chatcmpl-xxx"

        fake_client = mock.MagicMock()
        fake_client.chat.completions.create = mock.AsyncMock(return_value=fake_resp)
        mock_sdk.return_value = fake_client

        provider = OpenAICompatProvider(api_key="sk-test", model="gpt-4o-mini")
        resp = await provider.chat([{"role": "user", "content": "Hi"}])

        self.assertEqual(resp.content, "Hello!")
        self.assertEqual(resp.finish_reason, "stop")
        self.assertEqual(resp.usage["prompt_tokens"], 10)

    @mock.patch("step1.llm.AsyncOpenAI")
    async def test_chat_with_tool_calls(self, mock_sdk):
        fake_tc = mock.MagicMock()
        fake_tc.id = "call_1"
        fake_tc.function.name = "echo"
        fake_tc.function.arguments = json.dumps({"text": "hi"})

        fake_msg = mock.MagicMock()
        fake_msg.content = None
        fake_msg.tool_calls = [fake_tc]

        fake_choice = mock.MagicMock()
        fake_choice.finish_reason = "tool_calls"
        fake_choice.message = fake_msg

        fake_resp = mock.MagicMock()
        fake_resp.choices = [fake_choice]
        fake_resp.usage = None

        fake_client = mock.MagicMock()
        fake_client.chat.completions.create = mock.AsyncMock(return_value=fake_resp)
        mock_sdk.return_value = fake_client

        provider = OpenAICompatProvider(api_key="sk-test")
        resp = await provider.chat([{"role": "user", "content": "Hi"}])

        self.assertIsNone(resp.content)
        self.assertEqual(resp.finish_reason, "tool_calls")
        self.assertEqual(len(resp.tool_calls), 1)
        self.assertEqual(resp.tool_calls[0].name, "echo")
        self.assertEqual(resp.tool_calls[0].arguments["text"], "hi")

    @mock.patch("step1.llm.AsyncOpenAI")
    async def test_chat_with_tools_param(self, mock_sdk):
        """Verify tools kwarg is passed through to the SDK."""
        fake_msg = mock.MagicMock()
        fake_msg.content = "OK"
        fake_msg.tool_calls = None
        fake_choice = mock.MagicMock()
        fake_choice.finish_reason = "stop"
        fake_choice.message = fake_msg
        fake_resp = mock.MagicMock()
        fake_resp.choices = [fake_choice]
        fake_resp.usage = None
        fake_client = mock.MagicMock()
        fake_client.chat.completions.create = mock.AsyncMock(return_value=fake_resp)
        mock_sdk.return_value = fake_client

        provider = OpenAICompatProvider(api_key="sk-test")
        tools_def = [{"type": "function", "function": {"name": "echo", "parameters": {}}}]
        resp = await provider.chat(
            [{"role": "user", "content": "Hi"}],
            tools=tools_def,
        )
        self.assertEqual(resp.content, "OK")

    async def test_from_env_missing_key(self):
        os.environ.pop("OPENAI_API_KEY", None)
        with self.assertRaises(KeyError):
            OpenAICompatProvider.from_env()


if __name__ == "__main__":
    unittest.main()
