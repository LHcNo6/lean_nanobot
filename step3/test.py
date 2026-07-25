"""Tests for Step 3 — retry logic."""

import asyncio
import json
import os
import unittest
from unittest import mock

import httpx
import openai

from step3.llm import LLMResponse, RetryConfig
from step3.provider import LLMProvider, _is_retryable_exception
from step3.openai_compat_provider import OpenAICompatProvider


def _fake_request() -> httpx.Request:
    return httpx.Request("POST", "https://api.example.com/v1/chat/completions")


def _fake_response(status_code: int) -> httpx.Response:
    return httpx.Response(status_code, request=_fake_request())


# ---------------------------------------------------------------------------
# _is_retryable_exception unit tests
# ---------------------------------------------------------------------------

class TestIsRetryableException(unittest.TestCase):
    def test_timeout_is_retryable(self):
        self.assertTrue(_is_retryable_exception(asyncio.TimeoutError()))

    def test_api_connection_error_is_retryable(self):
        exc = openai.APIConnectionError(message="connection failed", request=_fake_request())
        self.assertTrue(_is_retryable_exception(exc))

    def test_api_timeout_error_is_retryable(self):
        exc = openai.APITimeoutError(request=_fake_request())
        self.assertTrue(_is_retryable_exception(exc))

    def test_rate_limit_is_retryable(self):
        exc = openai.RateLimitError(
            "rate limited",
            response=_fake_response(429),
            body=None,
        )
        self.assertTrue(_is_retryable_exception(exc))

    def test_internal_server_error_is_retryable(self):
        exc = openai.InternalServerError(
            "internal error",
            response=_fake_response(500),
            body=None,
        )
        self.assertTrue(_is_retryable_exception(exc))

    def test_auth_error_not_retryable(self):
        exc = openai.AuthenticationError(
            "auth failed",
            response=_fake_response(401),
            body=None,
        )
        self.assertFalse(_is_retryable_exception(exc))

    def test_generic_exception_not_retryable(self):
        self.assertFalse(_is_retryable_exception(Exception("anything")))

    def test_bad_request_not_retryable(self):
        exc = openai.BadRequestError(
            "bad request",
            response=_fake_response(400),
            body=None,
        )
        self.assertFalse(_is_retryable_exception(exc))


# ---------------------------------------------------------------------------
# Test provider that fails on demand
# ---------------------------------------------------------------------------

class _RetryTestProvider(LLMProvider):
    def __init__(self):
        self.chat_fails: list[Exception | None] = []
        self.chat_call_count = 0
        self.stream_fails: list[Exception | None] = []
        self.stream_call_count = 0
        self.stream_deltas_before_fail: list[list[str]] = []

    async def chat(self, **kwargs):
        idx = self.chat_call_count
        self.chat_call_count += 1
        if idx < len(self.chat_fails) and self.chat_fails[idx] is not None:
            raise self.chat_fails[idx]
        return LLMResponse(content="ok", finish_reason="stop")

    async def chat_stream(self, **kwargs):
        idx = self.stream_call_count
        self.stream_call_count += 1
        on_content_delta = kwargs.pop("on_content_delta", None)
        if idx < len(self.stream_fails) and self.stream_fails[idx] is not None:
            if idx < len(self.stream_deltas_before_fail):
                for d in self.stream_deltas_before_fail[idx]:
                    if on_content_delta:
                        await on_content_delta(d)
            raise self.stream_fails[idx]
        return LLMResponse(content="ok", finish_reason="stop")


# ---------------------------------------------------------------------------
# chat_with_retry tests
# ---------------------------------------------------------------------------

class TestChatWithRetry(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.provider = _RetryTestProvider()

    async def test_success_first_try(self):
        self.provider.chat_fails = [None]
        resp = await self.provider.chat_with_retry(
            [{"role": "user", "content": "Hi"}],
        )
        self.assertEqual(resp.content, "ok")
        self.assertEqual(self.provider.chat_call_count, 1)

    async def test_fail_then_succeed(self):
        exc = openai.APIConnectionError(message="connection reset", request=_fake_request())
        self.provider.chat_fails = [exc, None]
        resp = await self.provider.chat_with_retry(
            [{"role": "user", "content": "Hi"}],
            retry_config=RetryConfig(max_retries=3),
        )
        self.assertEqual(resp.content, "ok")
        self.assertEqual(self.provider.chat_call_count, 2)

    async def test_exhausted(self):
        exc = openai.RateLimitError(
            "rate limited",
            response=_fake_response(429),
            body=None,
        )
        self.provider.chat_fails = [exc, exc, exc, exc]
        with self.assertRaises(openai.RateLimitError):
            await self.provider.chat_with_retry(
                [{"role": "user", "content": "Hi"}],
                retry_config=RetryConfig(max_retries=2),
            )
        self.assertEqual(self.provider.chat_call_count, 3)

    async def test_non_retryable_propagates(self):
        exc = openai.AuthenticationError(
            "bad key",
            response=_fake_response(401),
            body=None,
        )
        self.provider.chat_fails = [exc]
        with self.assertRaises(openai.AuthenticationError):
            await self.provider.chat_with_retry(
                [{"role": "user", "content": "Hi"}],
                retry_config=RetryConfig(max_retries=3),
            )
        self.assertEqual(self.provider.chat_call_count, 1)


# ---------------------------------------------------------------------------
# chat_stream_with_retry tests
# ---------------------------------------------------------------------------

class TestChatStreamWithRetry(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.provider = _RetryTestProvider()

    async def test_stream_success_first_try(self):
        self.provider.stream_fails = [None]
        deltas: list[str] = []

        async def on_delta(text):
            deltas.append(text)

        resp = await self.provider.chat_stream_with_retry(
            [{"role": "user", "content": "Hi"}],
            on_content_delta=on_delta,
        )
        self.assertEqual(resp.content, "ok")
        self.assertEqual(self.provider.stream_call_count, 1)

    async def test_stream_fail_before_delta(self):
        exc = openai.APIConnectionError(message="network down", request=_fake_request())
        self.provider.stream_fails = [exc, None]
        self.provider.stream_deltas_before_fail = [[], []]

        resp = await self.provider.chat_stream_with_retry(
            [{"role": "user", "content": "Hi"}],
            retry_config=RetryConfig(max_retries=2),
        )
        self.assertEqual(resp.content, "ok")
        self.assertEqual(self.provider.stream_call_count, 2)

    async def test_stream_fail_after_delta_no_retry(self):
        exc = openai.RateLimitError(
            "rate limited",
            response=_fake_response(429),
            body=None,
        )
        self.provider.stream_fails = [exc]
        self.provider.stream_deltas_before_fail = [["partial "]]

        with self.assertRaises(openai.RateLimitError):
            await self.provider.chat_stream_with_retry(
                [{"role": "user", "content": "Hi"}],
                retry_config=RetryConfig(max_retries=2),
            )
        self.assertEqual(self.provider.stream_call_count, 1)

    async def test_stream_exhausted(self):
        exc = openai.APIConnectionError(message="network down", request=_fake_request())
        self.provider.stream_fails = [exc, exc, exc]
        self.provider.stream_deltas_before_fail = [[], [], []]

        with self.assertRaises(openai.APIConnectionError):
            await self.provider.chat_stream_with_retry(
                [{"role": "user", "content": "Hi"}],
                retry_config=RetryConfig(max_retries=1),
            )
        self.assertEqual(self.provider.stream_call_count, 2)

    async def test_stream_timeout_before_delta_retried(self):
        self.provider.stream_fails = [asyncio.TimeoutError(), None]
        self.provider.stream_deltas_before_fail = [[], []]

        resp = await self.provider.chat_stream_with_retry(
            [{"role": "user", "content": "Hi"}],
            retry_config=RetryConfig(max_retries=2),
        )
        self.assertEqual(resp.content, "ok")
        self.assertEqual(self.provider.stream_call_count, 2)


# ---------------------------------------------------------------------------
# MockStream helper for SDK integration tests
# ---------------------------------------------------------------------------

class _MockStream:
    def __init__(self, chunks: list):
        self._chunks = list(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)


def _chunk(content: str = "", finish: str | None = None,
           usage: mock.MagicMock | None = None) -> mock.MagicMock:
    delta = mock.MagicMock()
    delta.content = content or None
    delta.tool_calls = None
    delta.function_call = None

    choice = mock.MagicMock()
    choice.delta = delta
    choice.finish_reason = finish
    choice.index = 0

    chunk = mock.MagicMock()
    chunk.choices = [choice]
    chunk.usage = usage
    chunk.model = "test-model"
    return chunk


# ---------------------------------------------------------------------------
# OpenAICompatProvider integration tests
# ---------------------------------------------------------------------------

class TestOpenAICompatRetry(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        os.environ["OPENAI_API_KEY"] = "sk-test"

    def tearDown(self):
        os.environ.pop("OPENAI_API_KEY", None)

    @mock.patch("step3.openai_compat_provider.AsyncOpenAI")
    async def test_chat_with_retry_sdk_connection_error(self, mock_sdk):
        fake_client = mock.MagicMock()
        side_effects = [
            openai.APIConnectionError(message="connection reset", request=_fake_request()),
            mock.MagicMock(
                choices=[mock.MagicMock(
                    message=mock.MagicMock(
                        content="Hello", tool_calls=None,
                    ),
                    finish_reason="stop",
                )],
                usage=mock.MagicMock(prompt_tokens=5, completion_tokens=10),
            ),
        ]
        fake_client.chat.completions.create = mock.AsyncMock(side_effect=side_effects)
        mock_sdk.return_value = fake_client

        provider = OpenAICompatProvider(api_key="sk-test")
        resp = await provider.chat_with_retry(
            [{"role": "user", "content": "Hi"}],
            retry_config=RetryConfig(max_retries=2),
        )
        self.assertEqual(resp.content, "Hello")
        self.assertEqual(fake_client.chat.completions.create.call_count, 2)

    @mock.patch("step3.openai_compat_provider.AsyncOpenAI")
    async def test_chat_stream_with_retry_timeout(self, mock_sdk):
        class _TimeoutStream:
            def __aiter__(self):
                return self
            async def __anext__(self):
                raise asyncio.TimeoutError

        usg = mock.MagicMock()
        usg.prompt_tokens = 5
        usg.completion_tokens = 3

        fake_client = mock.MagicMock()
        side_effects = [
            _TimeoutStream(),
            _MockStream([_chunk("Hello", finish="stop", usage=usg)]),
        ]
        fake_client.chat.completions.create = mock.AsyncMock(side_effect=side_effects)
        mock_sdk.return_value = fake_client

        provider = OpenAICompatProvider(api_key="sk-test")
        deltas: list[str] = []

        async def on_delta(text):
            deltas.append(text)

        resp = await provider.chat_stream_with_retry(
            [{"role": "user", "content": "Hi"}],
            on_content_delta=on_delta,
            retry_config=RetryConfig(max_retries=2),
        )
        self.assertEqual(resp.content, "Hello")
        self.assertEqual(deltas, ["Hello"])
        self.assertEqual(fake_client.chat.completions.create.call_count, 2)

    @mock.patch("step3.openai_compat_provider.AsyncOpenAI")
    async def test_chat_non_retryable_auth_error(self, mock_sdk):
        fake_client = mock.MagicMock()
        fake_client.chat.completions.create = mock.AsyncMock(
            side_effect=openai.AuthenticationError(
                "bad key",
                response=_fake_response(401),
                body=None,
            ),
        )
        mock_sdk.return_value = fake_client

        provider = OpenAICompatProvider(api_key="sk-test")
        with self.assertRaises(openai.AuthenticationError):
            await provider.chat_with_retry(
                [{"role": "user", "content": "Hi"}],
                retry_config=RetryConfig(max_retries=3),
            )
        self.assertEqual(fake_client.chat.completions.create.call_count, 1)


if __name__ == "__main__":
    unittest.main()
