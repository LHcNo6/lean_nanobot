"""Tests for Step 2 — streaming support."""

import asyncio
import json
import os
import unittest
from unittest import mock

from step2.llm import LLMProvider, LLMResponse, ToolCallRequest, OpenAICompatProvider


class MockStream:
    """Async iterable that yields pre-defined chunks."""

    def __init__(self, chunks: list):
        self._chunks = list(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)


def _chunk_choice(delta_content: str = "", finish_reason: str | None = None,
                  tool_calls: list | None = None) -> mock.MagicMock:
    """Build a single ChatCompletionChunk.choices[0] mock."""
    delta = mock.MagicMock()
    delta.content = delta_content or None
    delta.tool_calls = tool_calls
    delta.function_call = None

    choice = mock.MagicMock()
    choice.delta = delta
    choice.finish_reason = finish_reason
    choice.index = 0
    return choice


def _chunk(content: str = "", finish: str | None = None,
           usage: mock.MagicMock | None = None,
           tool_calls: list | None = None) -> mock.MagicMock:
    """Build a ChatCompletionChunk mock."""
    chunk = mock.MagicMock()
    chunk.choices = [_chunk_choice(content, finish, tool_calls)] if content or finish or tool_calls else []
    chunk.usage = usage
    chunk.model = "test-model"
    return chunk


class TestDataTypes(unittest.TestCase):
    def test_tool_call_request(self):
        tc = ToolCallRequest(id="call_1", name="echo", arguments={"text": "hello"})
        self.assertEqual(tc.name, "echo")

    def test_llm_response(self):
        resp = LLMResponse(content="Hello", finish_reason="stop", usage={"prompt_tokens": 5})
        self.assertEqual(resp.content, "Hello")
        self.assertFalse(resp.tool_calls)


class TestLLMProviderBaseStream(unittest.IsolatedAsyncioTestCase):
    """Test the default chat_stream fallback behaviour."""

    async def test_default_fallback_calls_chat(self):
        class DummyProvider(LLMProvider):
            async def chat(self, messages, **kw):
                return LLMResponse(content="mock response", finish_reason="stop")

        provider = DummyProvider()
        deltas: list[str] = []

        async def on_delta(text):
            deltas.append(text)

        resp = await provider.chat_stream(
            [{"role": "user", "content": "Hi"}],
            on_content_delta=on_delta,
        )
        self.assertEqual(resp.content, "mock response")
        self.assertEqual(deltas, ["mock response"])

    async def test_default_no_delta_when_no_content(self):
        class DummyProvider(LLMProvider):
            async def chat(self, messages, **kw):
                return LLMResponse(content=None, finish_reason="stop")

        provider = DummyProvider()
        called = False

        async def on_delta(text):
            nonlocal called
            called = True

        await provider.chat_stream(
            [{"role": "user", "content": "Hi"}],
            on_content_delta=on_delta,
        )
        self.assertFalse(called)


class TestOpenAICompatProviderStream(unittest.IsolatedAsyncioTestCase):
    """Mock streaming responses from the OpenAI SDK."""

    def setUp(self):
        os.environ["OPENAI_API_KEY"] = "sk-test"

    def tearDown(self):
        os.environ.pop("OPENAI_API_KEY", None)

    @mock.patch("step2.llm.AsyncOpenAI")
    async def test_stream_text_content(self, mock_sdk):
        usg = mock.MagicMock()
        usg.prompt_tokens = 5
        usg.completion_tokens = 3

        chunks = [
            _chunk("Hello"),
            _chunk(" world"),
            _chunk("!", finish="stop", usage=usg),
        ]
        fake_client = mock.MagicMock()
        fake_client.chat.completions.create = mock.AsyncMock(
            return_value=MockStream(chunks),
        )
        mock_sdk.return_value = fake_client

        provider = OpenAICompatProvider(api_key="sk-test")
        deltas: list[str] = []

        async def on_delta(text):
            deltas.append(text)

        resp = await provider.chat_stream(
            [{"role": "user", "content": "Hi"}],
            on_content_delta=on_delta,
        )

        self.assertEqual(deltas, ["Hello", " world", "!"])
        self.assertEqual(resp.content, "Hello world!")
        self.assertEqual(resp.finish_reason, "stop")
        self.assertEqual(resp.usage["prompt_tokens"], 5)
        self.assertEqual(resp.usage["completion_tokens"], 3)

    @mock.patch("step2.llm.AsyncOpenAI")
    async def test_stream_tool_calls(self, mock_sdk):
        """Verify tool call deltas are accumulated correctly."""
        tc_delta_1 = mock.MagicMock()
        tc_delta_1.index = 0
        tc_delta_1.id = "call_1"
        tc_delta_1.function = mock.MagicMock()
        tc_delta_1.function.name = "echo"
        tc_delta_1.function.arguments = '{"text":'

        tc_delta_2 = mock.MagicMock()
        tc_delta_2.index = 0
        tc_delta_2.id = ""
        tc_delta_2.function = mock.MagicMock()
        tc_delta_2.function.name = ""
        tc_delta_2.function.arguments = ' "hi"}'

        usg = mock.MagicMock()
        usg.prompt_tokens = 5
        usg.completion_tokens = 2

        chunks = [
            _chunk(tool_calls=[tc_delta_1]),
            _chunk(tool_calls=[tc_delta_2]),
            _chunk(finish="tool_calls", usage=usg),
        ]
        fake_client = mock.MagicMock()
        fake_client.chat.completions.create = mock.AsyncMock(
            return_value=MockStream(chunks),
        )
        mock_sdk.return_value = fake_client

        provider = OpenAICompatProvider(api_key="sk-test")
        resp = await provider.chat_stream(
            [{"role": "user", "content": "Hi"}],
        )

        self.assertEqual(resp.finish_reason, "tool_calls")
        self.assertEqual(len(resp.tool_calls), 1)
        self.assertEqual(resp.tool_calls[0].name, "echo")
        self.assertEqual(resp.tool_calls[0].arguments["text"], "hi")

    @mock.patch("step2.llm.AsyncOpenAI")
    async def test_stream_timeout(self, mock_sdk):
        """Timeout should return finish_reason='error'."""
        class TimeoutStream:
            def __aiter__(self):
                return self
            async def __anext__(self):
                raise asyncio.TimeoutError

        fake_client = mock.MagicMock()
        fake_client.chat.completions.create = mock.AsyncMock(
            return_value=TimeoutStream(),
        )
        mock_sdk.return_value = fake_client

        provider = OpenAICompatProvider(api_key="sk-test")
        resp = await provider.chat_stream(
            [{"role": "user", "content": "Hi"}],
        )
        self.assertEqual(resp.finish_reason, "error")
        self.assertIsNone(resp.content)

    @mock.patch("step2.llm.AsyncOpenAI")
    async def test_stream_empty_chunks(self, mock_sdk):
        """No choices in any chunk should still produce a valid response."""
        usg = mock.MagicMock()
        usg.prompt_tokens = 0
        usg.completion_tokens = 0

        empty = mock.MagicMock()
        empty.choices = []
        empty.usage = usg

        fake_client = mock.MagicMock()
        fake_client.chat.completions.create = mock.AsyncMock(
            return_value=MockStream([empty]),
        )
        mock_sdk.return_value = fake_client

        provider = OpenAICompatProvider(api_key="sk-test")
        resp = await provider.chat_stream(
            [{"role": "user", "content": "Hi"}],
        )
        self.assertIsNone(resp.content)
        self.assertEqual(resp.finish_reason, "stop")


if __name__ == "__main__":
    unittest.main()
