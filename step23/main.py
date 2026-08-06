from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from step23.bus import MessageBus
from step23.context import ContextBuilder
from step23.llm import LLMRuntime, ModelPreset, resolve_preset
from step23.loop import AgentLoop
from step23.manager import ChannelManager
from step23.memory import MemoryStore
from step23.pairing import PairingStore
from step23.providers.factory import ProviderSettings, make_provider
from step23.session import SessionManager
from step23.subagent import SubagentManager
from step23.tool import ToolRegistry
from step23.tools.echo import EchoTool
from step23.tools.spawn import SpawnTool
from step23.tools.long_task import CreateGoalTool, UpdateGoalTool

_DEMO_CONTEXT_WINDOW = 1024
_DEMO_MAX_TOKENS = 128
_DREAM_INTERVAL_SECONDS = 300

_PRESETS = {
    "default": ModelPreset(
        name="default",
        model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
        provider=os.environ.get("OPENAI_PROVIDER") or None,
        context_window_tokens=_DEMO_CONTEXT_WINDOW,
        max_tokens=_DEMO_MAX_TOKENS,
    ),
}


def _build_settings(preset: ModelPreset) -> ProviderSettings:
    fallback_models = [
        m.strip()
        for m in os.environ.get("FALLBACK_MODELS", "").split(",")
        if m.strip()
    ]
    return ProviderSettings(
        model=preset.model,
        provider=preset.provider,
        api_key=os.environ.get("OPENAI_API_KEY"),
        api_base=os.environ.get("OPENAI_API_BASE"),
        temperature=preset.temperature,
        max_tokens=preset.max_tokens,
        context_window_tokens=preset.context_window_tokens,
        fallbacks=[
            ProviderSettings(
                model=m,
                api_key=os.environ.get("OPENAI_API_KEY"),
                api_base=os.environ.get("OPENAI_API_BASE"),
                max_tokens=preset.max_tokens,
            )
            for m in fallback_models
        ],
    )


async def _dream_loop(agent_loop: AgentLoop):
    while True:
        await asyncio.sleep(_DREAM_INTERVAL_SECONDS)
        result = await agent_loop.run_dream()
        if result and result.final_content:
            print(f"\n[Dream] {result.final_content[:200]}\n")


async def main():
    session_key = sys.argv[1] if len(sys.argv) > 1 else "default"
    identity = "You are lean_nanobot, a minimal AI agent with subagent and sustained goal support."

    preset = resolve_preset(_PRESETS, "default")
    provider = make_provider(_build_settings(preset))
    runtime = LLMRuntime.capture(
        provider=provider,
        model=preset.model,
        context_window_tokens=preset.context_window_tokens,
        max_tokens=preset.max_tokens,
        temperature=preset.temperature,
        model_preset="default",
    )

    registry = ToolRegistry()
    registry.register(EchoTool())

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

    pairing = PairingStore(path=Path("pairing.json"))

    agent_loop = AgentLoop(
        bus=bus,
        provider=provider,
        registry=registry,
        session_manager=session_manager,
        context_builder=context_builder,
        memory=memory,
        identity=identity,
        runtime=runtime,
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
    print(f"Provider: {preset.provider or '(auto)'}, model: {preset.model}")
    print(f"Context window: {runtime.context_window_tokens}, replay budget: {agent_loop.replay_budget}")
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
