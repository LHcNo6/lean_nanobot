#!/usr/bin/env python3
"""Bare HTTP POST to an OpenAI-compatible API.

Usage:
    OPENAI_API_KEY=xxx python main.py "Hello"
    OPENAI_API_KEY=xxx OPENAI_API_BASE=https://custom.api.com/v1 python main.py "Hi"
"""

import json
import os
import sys
import urllib.error
import urllib.request


def call_llm(message: str) -> dict:
    """Send a user message to the LLM and return the parsed JSON response."""
    api_key = os.environ.get("OPENAI_API_KEY", "")
    api_base = os.environ.get("OPENAI_API_BASE", "https://api.openai.com/v1")
    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

    if not api_key:
        raise RuntimeError("OPENAI_API_KEY environment variable not set")

    url = f"{api_base}/chat/completions"
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": message}],
        "temperature": 0.7,
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python main.py <message>")
        sys.exit(1)

    try:
        data = call_llm(sys.argv[1])
    except RuntimeError as e:
        print(f"Config Error: {e}")
        sys.exit(1)
    except urllib.error.HTTPError as e:
        print(f"HTTP Error {e.code}: {e.read().decode()}")
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)

    choice = data["choices"][0]
    finish_reason = choice["finish_reason"]
    usage = data.get("usage", {})

    print(f"\n[{finish_reason}]", flush=True)
    print(f"prompt_tokens={usage.get('prompt_tokens', '?')}, "
          f"completion_tokens={usage.get('completion_tokens', '?')}", flush=True)
    print(f"\n{choice['message']['content']}", flush=True)


if __name__ == "__main__":
    main()
