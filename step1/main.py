#!/usr/bin/env python3
"""CLI for Step 1 — uses the Provider abstraction."""

import asyncio
import sys

from step1.llm import OpenAICompatProvider


async def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python -m step1.main <message> [--system <prompt>]")
        sys.exit(1)

    message = sys.argv[1]
    system = None
    if "--system" in sys.argv:
        idx = sys.argv.index("--system")
        if idx + 1 < len(sys.argv):
            system = sys.argv[idx + 1]

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": message})

    provider = OpenAICompatProvider.from_env()
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
