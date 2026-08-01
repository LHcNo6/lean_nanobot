from __future__ import annotations

import asyncio
import sys

from step18.bus import MessageBus
from step18.consolidation import Consolidator
from step18.context import ContextBuilder
from step18.events import InboundMessage, OutboundMessage
from step18.goal_state import goal_state_runtime_lines, sustained_goal_active
from step18.loop import AgentLoop
from step18.memory import MemoryStore
from step18.openai_compat_provider import OpenAICompatProvider
from step18.session import SessionManager
from step18.subagent import SubagentManager
from step18.tool import ToolRegistry
from step18.tools.echo import EchoTool
from step18.tools.spawn import SpawnTool
from step18.tools.long_task import CreateGoalTool, UpdateGoalTool

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
    identity = "You are lean_nanobot, a minimal AI agent with subagent and sustained goal support."

    registry = ToolRegistry()
    registry.register(EchoTool())

    provider = OpenAICompatProvider.from_env()
    bus = MessageBus()
    memory = MemoryStore(workspace=".")
    session_manager = SessionManager(workspace=".")
    context_builder = ContextBuilder(workspace=".")

    subagent_manager = SubagentManager(
        bus=bus,
        provider=provider,
        tools=registry,
        max_concurrent_subagents=5,
    )

    spawn_tool = SpawnTool(manager=subagent_manager)
    create_goal_tool = CreateGoalTool(sessions=session_manager)
    update_goal_tool = UpdateGoalTool(sessions=session_manager)
    registry.register(spawn_tool)
    registry.register(create_goal_tool)
    registry.register(update_goal_tool)

    replay_budget = _DEMO_CONTEXT_WINDOW - _DEMO_MAX_TOKENS - _SAFETY_BUFFER

    agent_loop = AgentLoop(
        bus=bus,
        provider=provider,
        registry=registry,
        session_manager=session_manager,
        context_builder=context_builder,
        memory=memory,
        identity=identity,
        replay_budget=replay_budget,
        subagent_manager=subagent_manager,
    )
    loop_task = asyncio.create_task(agent_loop.run())
    dream_task = asyncio.create_task(_dream_loop(agent_loop))

    print(f"Session key: {session_key}")
    print(f"Context window: {_DEMO_CONTEXT_WINDOW}, replay budget: {replay_budget}")
    print(f"Dream interval: {_DREAM_INTERVAL_SECONDS}s")
    print("Type /exit to quit, /history to show history, /new to reset, /dream to trigger now")
    print("Commands: spawn <task> | /goal <objective>\n")

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
                goal_lines = goal_state_runtime_lines(session.metadata)
                if goal_lines:
                    print("  [goal] " + " | ".join(goal_lines[:2]))
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
