from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

from step92.bus import MessageBus
from step92.config import load_config, resolve_config_env_vars
from step92.context import ContextBuilder
from step92.llm import LLMRuntime
from step92.loop import AgentLoop
from step92.manager import ChannelManager
from step92.memory import MemoryStore
from step92.pairing import PairingStore
from step92.providers.factory import build_provider_snapshot
from step92.runner import AgentRunResult, AgentRunSpec
from step92.session import SessionManager
from step92.subagent import SubagentManager
from step92.tool import ToolRegistry
from step92.tools.echo import EchoTool
from step92.tools.long_task import CreateGoalTool, UpdateGoalTool
from step92.tools.spawn import SpawnTool

_CLI_DEFAULT_SECTION = {"enabled": True, "allow_from": ["*"], "streaming": True}


async def run_dream(loop: AgentLoop, tools: ToolRegistry | None = None) -> AgentRunResult | None:
    """后台记忆整理（harness 层，step64 从 loop.py 迁移）。

    构建 dream prompt，直接调用 runner.run，不经过 _process_message 状态机。
    迁移原因：dream 是 harness 级别的后台任务，不应耦合在 AgentLoop 中。

    Args:
        loop: AgentLoop 实例（提供 memory / runner / provider / registry）。
        tools: 自定义工具集；None 时使用 loop 默认 registry。

    Returns:
        AgentRunResult 或 None（无内容可整理或运行失败时）。
    """
    result = loop.memory.build_dream_prompt(max_entries=20)
    if result is None:
        return None
    prompt, last_cursor = result
    dream_key = f"dream:{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    spec = AgentRunSpec(
        initial_messages=[{"role": "user", "content": prompt}],
        tools=tools or loop.registry,
        provider=loop.provider,
        max_iterations=15,
        session_key=dream_key,
    )
    try:
        run_result = await loop._runner.run(spec)
        loop.memory.set_last_dream_cursor(last_cursor)
        return run_result
    except Exception:
        return None


async def _dream_loop(agent_loop: AgentLoop, interval_seconds: int):
    while True:
        await asyncio.sleep(interval_seconds)
        result = await run_dream(agent_loop)
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

    # step29 (A9 演示): 注册一个 loop 级运行时上下文提供器 —— 每个 turn 前
    # 动态解析当前时间并追加到用户消息尾部（对齐 nanobot register_runtime_context_provider）。
    async def _clock_runtime_context(request_context: Any):
        from step92.runtime_context import RuntimeContextBlock, wrap_runtime_context_lines

        lines = [f"Current UTC time: {datetime.now(timezone.utc).isoformat()}"]
        wrapped = wrap_runtime_context_lines(lines)
        return (
            RuntimeContextBlock(source="clock", content=wrapped)
            if request_context.session_key
            else None
        )

    agent_loop.register_runtime_context_provider(_clock_runtime_context)

    # step27 (H3 起步): 装配 harness 在 from_config 之上挂载运行时事件订阅。
    # 这里只是演示订阅者：打印 turn 生命周期。真实消费者（WebUI/状态机观测）
    # 后续 step 接入。
    async def _on_runtime_event(event) -> None:
        from step92.bus.runtime_events import (
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
    print(f"Workspace: {workspace} (restrict_to_workspace={config.tools.restrict_to_workspace})")
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
