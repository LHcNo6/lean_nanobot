#!/usr/bin/env python3
"""OpenAI SDK 调用 LLM，异步，支持多角色消息。

Usage:
    python main.py "你好"
    python main.py --system "你是助手" "你好"
"""

import argparse
import asyncio
import os

from dotenv import load_dotenv
from openai import AsyncOpenAI


load_dotenv()


def _client() -> AsyncOpenAI:
    return AsyncOpenAI(
        api_key=os.environ["OPENAI_API_KEY"],
        base_url=os.environ.get("OPENAI_API_BASE", "https://api.openai.com/v1"),
    )


async def call_llm(
    messages: list[dict],
    model: str | None = None,
) -> dict:
    """Send messages to the LLM and return the full API response dict."""
    client = _client()
    model = model or os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    resp = await client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0.7,
    )
    return resp.model_dump()


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("message", nargs="?", help="User message")
    parser.add_argument("--system", help="System prompt")
    args = parser.parse_args()

    if not args.message:
        parser.print_help()
        return

    messages = []
    if args.system:
        messages.append({"role": "system", "content": args.system})
    messages.append({"role": "user", "content": args.message})

    data = await call_llm(messages)

    choice = data["choices"][0]
    finish_reason = choice["finish_reason"]
    usage = data.get("usage", {})

    print(f"\n[{finish_reason}]", flush=True)
    print(
        f"prompt_tokens={usage.get('prompt_tokens', '?')}, "
        f"completion_tokens={usage.get('completion_tokens', '?')}",
        flush=True,
    )
    print(f"\n{choice['message']['content']}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
