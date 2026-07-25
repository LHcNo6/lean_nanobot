"""Tests for Step 0_1 — OpenAI SDK call."""

import os
import unittest
from unittest import mock


class TestCallLLM(unittest.IsolatedAsyncioTestCase):
    """Mock the OpenAI SDK so no real API key is needed."""

    def setUp(self):
        os.environ["OPENAI_API_KEY"] = "sk-test"
        os.environ.pop("OPENAI_API_BASE", None)
        os.environ.pop("OPENAI_MODEL", None)

    def tearDown(self):
        os.environ.pop("OPENAI_API_KEY", None)

    def _make_fake_completion(self, content: str, finish_reason: str = "stop"):
        """Build a fake SDK response object."""
        obj = mock.MagicMock()
        obj.model_dump.return_value = {
            "choices": [{
                "finish_reason": finish_reason,
                "message": {"role": "assistant", "content": content},
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            "model": "gpt-4o-mini",
            "id": "chatcmpl-xxx",
        }
        return obj

    @mock.patch("step0_1.main.AsyncOpenAI")
    async def test_call_llm_basic(self, mock_sdk):
        from step0_1.main import call_llm

        fake_completion = self._make_fake_completion("Hello!")
        fake_client = mock.MagicMock()
        fake_client.chat.completions.create = mock.AsyncMock(return_value=fake_completion)
        mock_sdk.return_value = fake_client

        data = await call_llm([{"role": "user", "content": "Hi"}])
        self.assertEqual(data["choices"][0]["message"]["content"], "Hello!")

    async def test_missing_key(self):
        os.environ.pop("OPENAI_API_KEY", None)
        from step0_1.main import call_llm
        with self.assertRaises(KeyError):
            await call_llm([{"role": "user", "content": "Hi"}])

    @mock.patch("step0_1.main.AsyncOpenAI")
    async def test_system_prompt(self, mock_sdk):
        from step0_1.main import call_llm

        fake_completion = self._make_fake_completion("OK")
        fake_client = mock.MagicMock()
        fake_client.chat.completions.create = mock.AsyncMock(return_value=fake_completion)
        mock_sdk.return_value = fake_client

        data = await call_llm([
            {"role": "system", "content": "Be concise."},
            {"role": "user", "content": "Hi"},
        ])
        _, kwargs = fake_client.chat.completions.create.call_args
        self.assertEqual(len(kwargs["messages"]), 2)
        self.assertEqual(kwargs["messages"][0]["role"], "system")
        self.assertEqual(data["choices"][0]["message"]["content"], "OK")


if __name__ == "__main__":
    unittest.main()
