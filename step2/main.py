#!/usr/bin/env python3
"""CLI for Step 2 — supports --stream for incremental token display."""

import asyncio
import sys

from step2.llm import OpenAICompatProvider


async def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python -m step2.main <message> [--system <prompt>] [--stream]")
        sys.exit(1)

    message = sys.argv[1]
    system = None
    stream = False

    if "--system" in sys.argv:
        idx = sys.argv.index("--system")
        if idx + 1 < len(sys.argv):
            system = sys.argv[idx + 1]
    if "--stream" in sys.argv:
        stream = True

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": message})

    provider = OpenAICompatProvider.from_env()

    if stream:

        async def on_delta(text: str) -> None:
            print(text, end="", flush=True)

        print("[streaming]", flush=True)
        resp = await provider.chat_stream(messages, on_content_delta=on_delta)
        print()
        print(f"\n[{resp.finish_reason}]", flush=True)
        print(
            f"prompt_tokens={resp.usage.get('prompt_tokens', '?')}, "
            f"completion_tokens={resp.usage.get('completion_tokens', '?')}",
            flush=True,
        )
    else:
        resp = await provider.chat(messages)
        print(f"\n[{resp.finish_reason}]", flush=True)
        print(
            f"prompt_tokens={resp.usage.get('prompt_tokens', '?')}, "
            f"completion_tokens={resp.usage.get('completion_tokens', '?')}",
            flush=True,
        )
        print(f"\n{resp.content}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
