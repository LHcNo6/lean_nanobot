"""Tests for Step 0 — mocked HTTP, no real API key needed."""

import json
import os
import sys
import unittest
from unittest import mock


class TestCallLLM(unittest.TestCase):
    """Test call_llm() with a fake HTTP response."""

    def setUp(self):
        os.environ["OPENAI_API_KEY"] = "test-key"

    def tearDown(self):
        os.environ.pop("OPENAI_API_KEY", None)

    @mock.patch("urllib.request.urlopen")
    def test_call_llm_success(self, mock_urlopen):
        fake_resp = {
            "choices": [{
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "Hello there!"},
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        cm = mock.MagicMock()
        cm.read.return_value = json.dumps(fake_resp).encode("utf-8")
        cm.__enter__.return_value = cm
        mock_urlopen.return_value = cm

        from step0.main import call_llm
        data = call_llm("Hi")
        self.assertEqual(data["choices"][0]["message"]["content"], "Hello there!")

    def test_call_llm_missing_key(self):
        os.environ.pop("OPENAI_API_KEY", None)
        from step0.main import call_llm
        with self.assertRaises(RuntimeError):
            call_llm("Hi")


class TestMain(unittest.TestCase):
    """Test the main() entry point."""

    @mock.patch("step0.main.call_llm")
    @mock.patch("step0.main.sys.argv", ["main.py", "hello"])
    def test_main_success(self, mock_call_llm):
        mock_call_llm.return_value = {
            "choices": [{
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "Hi!"},
            }],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2},
        }
        from step0.main import main
        main()  # should print and not raise


if __name__ == "__main__":
    unittest.main()
