from __future__ import annotations

import asyncio
import sys
from datetime import datetime

from step9.bus import MessageBus
from step9.consolidation import Consolidator
from step9.context import ContextBuilder
from step9.events import InboundMessage, OutboundMessage
from step9.openai_compat_provider import OpenAICompatProvider
from step9.runner import AgentRunSpec, AgentRunner
from step9.session import SessionManager
from step9.tool import ToolRegistry
from step9.tools.echo import EchoTool

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


async def _agent_loop(
    bus: MessageBus,
    session_key: str,
    provider: OpenAICompatProvider,
    registry: ToolRegistry,
    session_manager: SessionManager,
    context_builder: ContextBuilder,
    consolidator: Consolidator,
    identity: str,
    replay_budget: int,
) -> None:
    while True:
        msg = await bus.consume_inbound()

        if msg.metadata.get("command") == "exit":
            await bus.publish_outbound(OutboundMessage(content="", metadata={"command": "exit"}))
            break

        if msg.metadata.get("command") == "new":
            session_manager._cache.pop(session_key, None)
            path = session_manager._session_path(session_key)
            if path.exists():
                path.unlink()
            await bus.publish_outbound(OutboundMessage(content="Session reset.", metadata={"command": "history_done"}))
            continue

        if msg.metadata.get("command") == "history":
            session = session_manager.get_or_create(session_key)
            lines = ["\n--- Session History ---"]
            for i, m in enumerate(session.messages):
                role = m["role"].ljust(9)
                content = (m.get("content") or "")[:80]
                name = m.get("name", "")
                extra = f" ({name})" if name else ""
                lc = " <-- last_consolidated" if i == session.last_consolidated else ""
                lines.append(f"  [{i}] {role}{content}{extra}{lc}")
            lines.append("---\n")
            await bus.publish_outbound(OutboundMessage(content="\n".join(lines), metadata={"command": "history_done"}))
            continue

        session = session_manager.get_or_create(session_key)

        summary = await consolidator.maybe_consolidate(
            session, max_tokens=replay_budget, model=provider.model,
        )

        history = session.get_history(max_messages=50, max_tokens=replay_budget)
        spec = AgentRunSpec(
            initial_messages=context_builder.build_messages(
                current_message=msg.content,
                history=history,
                identity=identity,
                session_summary=summary,
            ),
            tools=registry,
            provider=provider,
            max_iterations=5,
        )
        result = await AgentRunner().run(spec)

        skip = 1 + len(history)
        session.import_messages(result.messages[skip:])
        session_manager.save(session)

        meta = {
            "stop_reason": result.stop_reason,
            "tokens": f"{result.total_prompt_tokens}+{result.total_completion_tokens}",
        }
        await bus.publish_outbound(OutboundMessage(content=result.final_content, metadata=meta))


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

    agent = asyncio.create_task(_agent_loop(
        bus=bus,
        session_key=session_key,
        provider=provider,
        registry=registry,
        session_manager=session_manager,
        context_builder=context_builder,
        consolidator=consolidator,
        identity=identity,
        replay_budget=replay_budget,
    ))

    print(f"Session key: {session_key}")
    print(f"Context window: {_DEMO_CONTEXT_WINDOW}, replay budget: {replay_budget}")
    print("Type /exit to quit, /history to show history, /new to reset\n")

    try:
        while True:
            text = await ainput("You: ")

            if text.lower() == "/exit":
                await bus.publish_inbound(InboundMessage(content="", metadata={"command": "exit"}))
                resp = await bus.consume_outbound()
                if resp.metadata.get("command") == "exit":
                    break
                continue

            if text.lower() == "/history":
                await bus.publish_inbound(InboundMessage(content="", metadata={"command": "history"}))
                resp = await bus.consume_outbound()
                print(resp.content)
                continue

            if text.lower() == "/new":
                await bus.publish_inbound(InboundMessage(content="", metadata={"command": "new"}))
                resp = await bus.consume_outbound()
                print(resp.content)
                continue

            await bus.publish_inbound(InboundMessage(content=text, chat_id=session_key))
            resp = await bus.consume_outbound()

            print(f"\n[{resp.metadata.get('stop_reason', '?')}]", flush=True)
            print(f"{resp.content}", flush=True)
            tokens = resp.metadata.get("tokens", "?")
            print(f"  tokens: {tokens}", flush=True)
            print()
    finally:
        agent.cancel()
        try:
            await agent
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    asyncio.run(main())
