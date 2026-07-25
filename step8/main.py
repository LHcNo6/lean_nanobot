#!/usr/bin/env python3
"""CLI for Step 8 — Token-budget-aware multi-turn session with consolidation."""

import asyncio
import sys

from step8.consolidation import Consolidator
from step8.context import ContextBuilder
from step8.openai_compat_provider import OpenAICompatProvider
from step8.runner import AgentRunSpec, AgentRunner
from step8.session import SessionManager
from step8.tool import ToolRegistry
from step8.tools.echo import EchoTool


# Simulate a small context window to trigger consolidation in demos
_DEMO_CONTEXT_WINDOW = 1024
_SAFETY_BUFFER = 128
_DEMO_MAX_TOKENS = 128


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
        lc = " <-- last_consolidated" if i == session.last_consolidated else ""
        print(f"  [{i}] {role}{content}{extra}{lc}")
    print("---\n")


async def main() -> None:
    session_key = sys.argv[1] if len(sys.argv) > 1 else "default"
    identity = "You are lean_nanobot, a minimal AI agent learning consolidation."

    registry = ToolRegistry()
    registry.register(EchoTool())

    provider = OpenAICompatProvider.from_env()
    consolidator = Consolidator(provider=provider)
    session_manager = SessionManager(workspace=".")
    context = ContextBuilder(workspace=".")

    replay_budget = _DEMO_CONTEXT_WINDOW - _DEMO_MAX_TOKENS - _SAFETY_BUFFER

    print(f"Session key: {session_key}")
    print(f"Context window: {_DEMO_CONTEXT_WINDOW}, replay budget: {replay_budget}")
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

        summary = await consolidator.maybe_consolidate(
            session,
            max_tokens=replay_budget,
            model=provider.model,
        )
        if summary:
            print(f"  [Consolidated {session.last_consolidated} messages, summary stored]")

        history = session.get_history(max_messages=50, max_tokens=replay_budget)
        spec = AgentRunSpec(
            initial_messages=context.build_messages(
                current_message=message,
                history=history,
                identity=identity,
                session_summary=summary,
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

        skip = 1 + len(history)
        session.import_messages(result.messages[skip:])
        session_manager.save(session)
        print()


if __name__ == "__main__":
    asyncio.run(main())
