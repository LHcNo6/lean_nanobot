from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from step27.bus import MessageBus
from step27.config import load_config, resolve_config_env_vars
from step27.context import ContextBuilder
from step27.llm import LLMRuntime
from step27.loop import AgentLoop
from step27.manager import ChannelManager
from step27.memory import MemoryStore
from step27.pairing import PairingStore
from step27.providers.factory import build_provider_snapshot
from step27.session import SessionManager
from step27.subagent import SubagentManager
from step27.tool import ToolRegistry
from step27.tools.echo import EchoTool
from step27.tools.long_task import CreateGoalTool, UpdateGoalTool
from step27.tools.spawn import SpawnTool

_CLI_DEFAULT_SECTION = {"enabled": True, "allow_from": ["*"], "streaming": True}


async def _dream_loop(agent_loop: AgentLoop, interval_seconds: int):
    while True:
        await asyncio.sleep(interval_seconds)
        result = await agent_loop.run_dream()
        if result and result.final_content:
            print(f"\n[Dream] {result.final_content[:200]}\n")


async def main():
    session_key = sys.argv[1] if len(sys.argv) > 1 else "default"

    config = load_config()
    config = resolve_config_env_vars(config)
    defaults = config.agents.defaults
    preset = config.resolve_preset()

    snapshot = build_provider_snapshot(config)
    runtime = LLMRuntime.capture(
        provider=snapshot.provider,
        model=snapshot.model,
        context_window_tokens=snapshot.context_window_tokens,
        max_tokens=preset.max_tokens,
        temperature=preset.temperature,
        model_preset=defaults.model_preset,
        snapshot_signature=snapshot.signature,
    )

    registry = ToolRegistry()
    registry.register(EchoTool())

    bus = MessageBus()
    workspace = str(config.workspace_path)
    memory = MemoryStore(workspace=workspace)
    session_manager = SessionManager(workspace=workspace)
    context_builder = ContextBuilder(
        workspace=workspace,
        disabled_skills=list(defaults.disabled_skills),
    )

    subagent_manager = SubagentManager(
        bus=bus,
        provider=snapshot.provider,
        tools=registry,
        max_concurrent_subagents=defaults.max_concurrent_subagents,
    )

    registry.register(SpawnTool(manager=subagent_manager))
    registry.register(CreateGoalTool(sessions=session_manager))
    registry.register(UpdateGoalTool(sessions=session_manager))

    pairing = PairingStore(path=Path("pairing.json"))

    agent_loop = AgentLoop.from_config(
        config,
        bus=bus,
        provider=snapshot.provider,
        runtime=runtime,
        registry=registry,
        session_manager=session_manager,
        context_builder=context_builder,
        memory=memory,
        subagent_manager=subagent_manager,
        pairing=pairing,
        identity=f"You are {defaults.bot_name}, a minimal AI agent with subagent and sustained goal support.",
    )

    # step27 (H3 起步): 装配 harness 在 from_config 之上挂载运行时事件订阅。
    # 这里只是演示订阅者：打印 turn 生命周期。真实消费者（WebUI/状态机观测）
    # 后续 step 接入。
    async def _on_runtime_event(event) -> None:
        from step27.bus.runtime_events import (
            SessionTurnStarted,
            TurnCompleted,
            TurnRunStatusChanged,
        )

        if isinstance(event, SessionTurnStarted):
            print(f"[runtime] turn started: {event.context.session_key}")
        elif isinstance(event, TurnRunStatusChanged):
            print(f"[runtime] run status: {event.context.session_key} -> {event.status}")
        elif isinstance(event, TurnCompleted):
            print(
                f"[runtime] turn completed: {event.context.session_key} "
                f"latency={event.latency_ms}ms"
            )

    agent_loop.runtime_events.subscribe(_on_runtime_event)

    channel_sections = dict(_CLI_DEFAULT_SECTION)
    channel_sections.update(config.channels.channel_sections())
    manager = ChannelManager(
        config=channel_sections,
        bus=bus,
        pairing=pairing,
    )

    cli_channel = manager.get_channel("cli")
    if cli_channel is not None:
        cli_channel.chat_id = session_key

    print(f"Session key: {session_key}")
    print(f"Provider: {preset.provider or '(auto)'}, model: {preset.model}")
    print(f"Context window: {runtime.context_window_tokens}, replay budget: {agent_loop.replay_budget}")
    print(f"Workspace: {workspace}")
    print(f"Dream interval: {defaults.dream.interval_seconds}s")
    print("Type /exit to quit, /help for commands (history, new, dream, pairing)")

    loop_task = asyncio.create_task(agent_loop.run())
    dream_task = asyncio.create_task(
        _dream_loop(agent_loop, defaults.dream.interval_seconds)
    )
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
