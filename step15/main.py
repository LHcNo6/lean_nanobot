from __future__ import annotations

import asyncio
import sys

from step15.bus import MessageBus
from step15.consolidation import Consolidator
from step15.context import ContextBuilder
from step15.events import InboundMessage, OutboundMessage
from step15.loop import AgentLoop
from step15.memory import MemoryStore
from step15.openai_compat_provider import OpenAICompatProvider
from step15.session import SessionManager
from step15.tool import ToolRegistry
from step15.tools.echo import EchoTool

_DEMO_CONTEXT_WINDOW = 1024
_SAFETY_BUFFER = 128
_DEMO_MAX_TOKENS = 128
_DREAM_INTERVAL_SECONDS = 300


async def ainput(prompt: str = "") -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, input, prompt)


async def _dream_loop(agent_loop: AgentLoop):
    while True:
        await asyncio.sleep(_DREAM_INTERVAL_SECONDS)
        result = await agent_loop.run_dream()
        if result and result.final_content:
            print(f"\n[Dream] {result.final_content[:200]}\n")


async def main():
    session_key = sys.argv[1] if len(sys.argv) > 1 else "default"
    identity = "You are lean_nanobot, a minimal AI agent learning consolidation."

    registry = ToolRegistry()
    registry.register(EchoTool())

    provider = OpenAICompatProvider.from_env()
    memory = MemoryStore(workspace=".")
    session_manager = SessionManager(workspace=".")
    context_builder = ContextBuilder(workspace=".")

    replay_budget = _DEMO_CONTEXT_WINDOW - _DEMO_MAX_TOKENS - _SAFETY_BUFFER
    bus = MessageBus()

    agent_loop = AgentLoop(
        bus=bus,
        provider=provider,
        registry=registry,
        session_manager=session_manager,
        context_builder=context_builder,
        memory=memory,
        identity=identity,
        replay_budget=replay_budget,
    )
    loop_task = asyncio.create_task(agent_loop.run())
    dream_task = asyncio.create_task(_dream_loop(agent_loop))

    print(f"Session key: {session_key}")
    print(f"Context window: {_DEMO_CONTEXT_WINDOW}, replay budget: {replay_budget}")
    print(f"Dream interval: {_DREAM_INTERVAL_SECONDS}s")
    print("Type /exit to quit, /history to show history, /new to reset, /dream to trigger now\n")

    try:
        while True:
            text = await ainput("You: ")

            if text.lower() == "/exit":
                agent_loop.stop()
                break

            if text.lower() == "/dream":
                result = await agent_loop.run_dream()
                if result and result.final_content:
                    print(f"\n[Dream result]\n{result.final_content[:300]}\n")
                else:
                    print("\n[Dream] Nothing to process.\n")
                continue

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
        agent_loop.stop()
        loop_task.cancel()
        dream_task.cancel()
        for t in (loop_task, dream_task):
            try:
                await t
            except asyncio.CancelledError:
                pass


if __name__ == "__main__":
    asyncio.run(main())