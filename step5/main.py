#!/usr/bin/env python3
"""CLI for Step 5 — full tool-calling loop with a real LLM provider."""

import asyncio
import sys

from step5.openai_compat_provider import OpenAICompatProvider
from step5.runner import AgentRunSpec, AgentRunner
from step5.tool import ToolRegistry
from step5.tools.echo import EchoTool


async def main() -> None:
    message = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "Say 'hello' using the echo tool"

    registry = ToolRegistry()
    registry.register(EchoTool())

    provider = OpenAICompatProvider.from_env()

    spec = AgentRunSpec(
        initial_messages=[{"role": "user", "content": message}],
        tools=registry,
        provider=provider,
        max_iterations=5,
    )

    result = await AgentRunner().run(spec)

    print(f"\n[{result.stop_reason}]", flush=True)
    print(f"\n{result.final_content}", flush=True)
    print(f"\nprompt_tokens={result.total_prompt_tokens}, "
          f"completion_tokens={result.total_completion_tokens}", flush=True)
    if result.tools_used:
        print(f"tools_used={result.tools_used}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
