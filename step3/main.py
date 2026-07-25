#!/usr/bin/env python3
"""CLI for Step 3 — supports --stream and --retry."""

import asyncio
import sys

from step3.openai_compat_provider import OpenAICompatProvider


async def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python -m step3.main <message> [--system <prompt>] [--stream] [--retry]")
        sys.exit(1)

    message = sys.argv[1]
    system = None
    stream = "--stream" in sys.argv
    retry = "--retry" in sys.argv

    if "--system" in sys.argv:
        idx = sys.argv.index("--system")
        if idx + 1 < len(sys.argv):
            system = sys.argv[idx + 1]

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": message})

    provider = OpenAICompatProvider.from_env()

    if stream:

        async def on_delta(text: str) -> None:
            print(text, end="", flush=True)

        print("[streaming]", flush=True)
        if retry:
            resp = await provider.chat_stream_with_retry(messages, on_content_delta=on_delta)
        else:
            resp = await provider.chat_stream(messages, on_content_delta=on_delta)
        print()
        print(f"\n[{resp.finish_reason}]", flush=True)
    else:
        if retry:
            resp = await provider.chat_with_retry(messages)
        else:
            resp = await provider.chat(messages)
        print(f"\n[{resp.finish_reason}]", flush=True)
        print(f"\n{resp.content}", flush=True)

    print(
        f"prompt_tokens={resp.usage.get('prompt_tokens', '?')}, "
        f"completion_tokens={resp.usage.get('completion_tokens', '?')}",
        flush=True,
    )


if __name__ == "__main__":
    asyncio.run(main())
