#!/usr/bin/env python3
"""CLI for Step 7 — multi-turn session with persistence."""

import asyncio
import sys

from step7.context import ContextBuilder
from step7.openai_compat_provider import OpenAICompatProvider
from step7.runner import AgentRunSpec, AgentRunner
from step7.session import SessionManager
from step7.tool import ToolRegistry
from step7.tools.echo import EchoTool


async def ainput(prompt: str = "") -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, input, prompt)


def print_history(session) -> None:
    print("\n--- Session History ---")
    for i, msg in enumerate(session.messages):
        role = msg["role"].ljust(9)
        content = (msg.get("content") or "")[:80]
        name = msg.get("name", "")
        extra = f" ({name})" if name else ""
        print(f"  [{i}] {role}{content}{extra}")
    print("---\n")


async def main() -> None:
    session_key = sys.argv[1] if len(sys.argv) > 1 else "default"
    identity = sys.argv[2] if len(sys.argv) > 2 else "You are lean_nanobot, a minimal AI agent."

    registry = ToolRegistry()
    registry.register(EchoTool())

    provider = OpenAICompatProvider.from_env()
    session_manager = SessionManager(workspace=".")
    context = ContextBuilder(workspace=".")

    print(f"Session key: {session_key}")
    print("Type /exit to quit, /history to show history, /new to reset\n")

    while True:
        message = await ainput("You: ")
        if message.lower() == "/exit":
            break
        if message.lower() == "/history":
            session = session_manager.get_or_create(session_key)
            print_history(session)
            continue
        if message.lower() == "/new":
            session_manager._cache.pop(session_key, None)
            path = session_manager._session_path(session_key)
            if path.exists():
                path.unlink()
            print("Session reset.\n")
            continue

        session = session_manager.get_or_create(session_key)
        history = session.get_history(max_messages=20)
        spec = AgentRunSpec(
            initial_messages=context.build_messages(
                current_message=message,
                history=history,
                identity=identity,
            ),
            tools=registry,
            provider=provider,
            max_iterations=5,
        )
        result = await AgentRunner().run(spec)

        print(f"\n[{result.stop_reason}]", flush=True)
        print(f"{result.final_content}", flush=True)
        print(f"  tokens: {result.total_prompt_tokens}+{result.total_completion_tokens}", flush=True)
        if result.tools_used:
            print(f"  tools: {result.tools_used}", flush=True)

        # Persist
        skip = 1 + len(history)
        session.import_messages(result.messages[skip:])
        session_manager.save(session)
        print()


if __name__ == "__main__":
    asyncio.run(main())
