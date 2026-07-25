#!/usr/bin/env python3
"""CLI for Step 6 — ContextBuilder + AgentRunner tool-calling loop."""

import asyncio
import sys

from step6.context import ContextBuilder
from step6.openai_compat_provider import OpenAICompatProvider
from step6.runner import AgentRunSpec, AgentRunner
from step6.tool import ToolRegistry
from step6.tools.echo import EchoTool


async def main() -> None:
    message = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "Say 'hello' using the echo tool"

    registry = ToolRegistry()
    registry.register(EchoTool())

    provider = OpenAICompatProvider.from_env()

    context = ContextBuilder(workspace=".")
    spec = AgentRunSpec(
        initial_messages=context.build_messages(
            current_message=message,
            identity="You are lean_nanobot, a minimal AI agent learning to use tools.",
        ),
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
