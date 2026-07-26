from __future__ import annotations

import asyncio
import sys

from step12.bus import MessageBus
from step12.consolidation import Consolidator
from step12.context import ContextBuilder
from step12.events import InboundMessage, OutboundMessage
from step12.loop import AgentLoop
from step12.openai_compat_provider import OpenAICompatProvider
from step12.session import SessionManager
from step12.tool import ToolRegistry
from step12.tools.echo import EchoTool

_DEMO_CONTEXT_WINDOW = 1024
_SAFETY_BUFFER = 128
_DEMO_MAX_TOKENS = 128


async def ainput(prompt: str = "") -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, input, prompt)


async def main() -> None:
    session_key = sys.argv[1] if len(sys.argv) > 1 else "default"
    identity = "You are lean_nanobot, a minimal AI agent learning consolidation."

    registry = ToolRegistry()
    registry.register(EchoTool())

    provider = OpenAICompatProvider.from_env()
    consolidator = Consolidator(provider=provider)
    session_manager = SessionManager(workspace=".")
    context_builder = ContextBuilder(workspace=".")

    replay_budget = _DEMO_CONTEXT_WINDOW - _DEMO_MAX_TOKENS - _SAFETY_BUFFER
    bus = MessageBus()

    loop = AgentLoop(
        bus=bus,
        provider=provider,
        registry=registry,
        session_manager=session_manager,
        context_builder=context_builder,
        consolidator=consolidator,
        identity=identity,
        replay_budget=replay_budget,
    )
    loop_task = asyncio.create_task(loop.run())

    print(f"Session key: {session_key}")
    print(f"Context window: {_DEMO_CONTEXT_WINDOW}, replay budget: {replay_budget}")
    print("Type /exit to quit, /history to show history, /new to reset\n")

    try:
        while True:
            text = await ainput("You: ")

            if text.lower() == "/exit":
                loop.stop()
                break

            if text.lower() == "/history":
                session = session_manager.get_or_create(session_key)
                print("\n--- Session History ---")
                for i, m in enumerate(session.messages):
                    role = m["role"].ljust(9)
                    content = (m.get("content") or "")[:80]
                    name = m.get("name", "")
                    extra = f" ({name})" if name else ""
                    lc = " <-- last_consolidated" if i == session.last_consolidated else ""
                    print(f"  [{i}] {role}{content}{extra}{lc}")
                print("---\n")
                continue

            if text.lower() == "/new":
                session_manager._cache.pop(session_key, None)
                path = session_manager._session_path(session_key)
                if path.exists():
                    path.unlink()
                print("Session reset.\n")
                continue

            await bus.publish_inbound(InboundMessage(content=text, chat_id=session_key))
            resp = await bus.consume_outbound()

            print(f"\n[{resp.metadata.get('stop_reason', '?')}]", flush=True)
            print(f"{resp.content}", flush=True)
            tokens = resp.metadata.get("tokens", "?")
            print(f"  tokens: {tokens}", flush=True)
            print()
    finally:
        loop.stop()
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    asyncio.run(main())
