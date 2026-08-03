from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from step21.bus import MessageBus
from step21.context import ContextBuilder
from step21.loop import AgentLoop
from step21.manager import ChannelManager
from step21.memory import MemoryStore
from step21.openai_compat_provider import OpenAICompatProvider
from step21.pairing import PairingStore
from step21.session import SessionManager
from step21.subagent import SubagentManager
from step21.tool import ToolRegistry
from step21.tools.echo import EchoTool
from step21.tools.spawn import SpawnTool
from step21.tools.long_task import CreateGoalTool, UpdateGoalTool

_DEMO_CONTEXT_WINDOW = 1024
_SAFETY_BUFFER = 128
_DEMO_MAX_TOKENS = 128
_DREAM_INTERVAL_SECONDS = 300


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

    pairing = PairingStore(path=Path("pairing.json"))

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
        pairing=pairing,
    )

    manager = ChannelManager(
        config={"cli": {"enabled": True, "allow_from": ["*"], "streaming": True}},
        bus=bus,
        pairing=pairing,
    )

    cli_channel = manager.get_channel("cli")
    if cli_channel is not None:
        cli_channel.chat_id = session_key

    print(f"Session key: {session_key}")
    print(f"Context window: {_DEMO_CONTEXT_WINDOW}, replay budget: {replay_budget}")
    print(f"Dream interval: {_DREAM_INTERVAL_SECONDS}s")
    print("Type /exit to quit, /help for commands (history, new, dream, pairing)")

    loop_task = asyncio.create_task(agent_loop.run())
    dream_task = asyncio.create_task(_dream_loop(agent_loop))
    try:
        await manager.start_all()
    finally:
        agent_loop.stop()
        loop_task.cancel()
        dream_task.cancel()
        await manager.stop_all()
        for t in (loop_task, dream_task):
            try:
                await t
            except asyncio.CancelledError:
                pass


if __name__ == "__main__":
    asyncio.run(main())
