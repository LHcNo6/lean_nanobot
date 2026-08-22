"""Step24 tests: session persistence sanitization + runtime checkpoint (A4 + A5).

All tests use mocked providers / constructed data; no real API keys.
"""

import asyncio
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from step96.bus import MessageBus
from step96.context import ContextBuilder
from step96.bus.events import InboundMessage
from step96.llm import LLMResponse, ToolCallRequest
from step96.loop import AgentLoop, TurnContext
from step96.memory import MemoryStore
from step96.provider import LLMProvider
from step96.runner import AgentRunSpec, AgentRunner
from step96.session import Session, SessionManager
from step96.tool import ToolRegistry, Tool, ToolResult
from step96.tools.echo import EchoTool


def _mk_loop() -> AgentLoop:
    """Bare AgentLoop instance for pure _save_turn / checkpoint unit tests."""
    loop = AgentLoop.__new__(AgentLoop)
    loop.max_tool_result_chars = 16_000
    return loop


def _make_full_loop(tmp_path) -> AgentLoop:
    bus = MessageBus()
    provider = _ScriptedProvider([])
    registry = ToolRegistry()
    registry.register(EchoTool())
    session_manager = SessionManager(workspace=str(tmp_path))
    context_builder = ContextBuilder(workspace=str(tmp_path))
    memory = MemoryStore(workspace=str(tmp_path))
    return AgentLoop(
        bus=bus, provider=provider, registry=registry,
        session_manager=session_manager, context_builder=context_builder,
        memory=memory, identity="You are a test bot.",
        replay_budget=10_000,
    )


class _ScriptedProvider(LLMProvider):
    """Provider that replays a fixed script of responses."""

    def __init__(self, responses: list[LLMResponse]):
        super().__init__()
        self._responses = list(responses)

    @property
    def model(self) -> str:
        return "mock-model"

    async def chat(self, messages, tools=None, model=None, temperature=0.7, max_tokens=4096):
        assert self._responses, "provider script exhausted"
        return self._responses.pop(0)


def _tool_call(tool_id: str, name: str = "echo", args: dict | None = None) -> ToolCallRequest:
    return ToolCallRequest(id=tool_id, name=name, arguments=args or {"text": "hi"})


def _assistant_with_tool_calls(tool_ids: list[str]) -> dict:
    return {
        "role": "assistant",
        "content": "working",
        "tool_calls": [
            {
                "id": tool_id,
                "type": "function",
                "function": {"name": "echo", "arguments": '{"text": "hi"}'},
            }
            for tool_id in tool_ids
        ],
    }


# ---------------------------------------------------------------------------
# _save_turn: persistence sanitization (A4)
# ---------------------------------------------------------------------------


def test_save_turn_skips_empty_assistant():
    loop = _mk_loop()
    session = Session(key="test:empty")
    session.add_message("user", "hi")

    loop._save_turn(
        session,
        [{"role": "assistant", "content": ""}],
        skip=0,
    )
    assert [m["role"] for m in session.messages] == ["user"]


def test_save_turn_keeps_assistant_with_tool_calls_even_when_content_empty():
    loop = _mk_loop()
    session = Session(key="test:tool-calls-only")

    loop._save_turn(
        session,
        [_assistant_with_tool_calls(["call_a"])],
        skip=0,
    )
    assert [m["role"] for m in session.messages] == ["assistant"]


def test_save_turn_drops_orphaned_tool_results():
    loop = _mk_loop()
    session = Session(key="test:orphan")
    session.add_message("user", "hi")

    loop._save_turn(
        session,
        [
            {"role": "tool", "tool_call_id": "call_ghost", "name": "echo", "content": "boo"},
            {"role": "assistant", "content": "done"},
        ],
        skip=0,
    )
    assert [m["role"] for m in session.messages] == ["user", "assistant"]
    assert session.messages[-1]["content"] == "done"


def test_save_turn_drops_tool_results_without_tool_call_id():
    loop = _mk_loop()
    session = Session(key="test:no-id")
    session.add_message("user", "hi")

    loop._save_turn(
        session,
        [
            {"role": "tool", "name": "echo", "content": "missing id"},
            {"role": "assistant", "content": "done"},
        ],
        skip=0,
    )
    assert [m["role"] for m in session.messages] == ["user", "assistant"]


def test_save_turn_keeps_tool_results_declared_in_prior_history():
    loop = _mk_loop()
    session = Session(key="test:prior-declared")
    session.add_message("assistant", "working", tool_calls=[{
        "id": "call_prior",
        "type": "function",
        "function": {"name": "echo", "arguments": "{}"},
    }])

    loop._save_turn(
        session,
        [{"role": "tool", "tool_call_id": "call_prior", "name": "echo", "content": "ok"}],
        skip=0,
    )
    assert [m["role"] for m in session.messages] == ["assistant", "tool"]


def test_save_turn_keeps_tool_results_declared_in_same_turn():
    loop = _mk_loop()
    session = Session(key="test:same-turn")

    loop._save_turn(
        session,
        [
            _assistant_with_tool_calls(["call_same"]),
            {"role": "tool", "tool_call_id": "call_same", "name": "echo", "content": "ok"},
        ],
        skip=0,
    )
    assert [m["role"] for m in session.messages] == ["assistant", "tool"]


def test_save_turn_truncates_oversized_tool_result():
    loop = _mk_loop()
    loop.max_tool_result_chars = 1_000
    session = Session(key="test:truncate")
    session.add_message("assistant", "working", tool_calls=[{
        "id": "call_big",
        "type": "function",
        "function": {"name": "echo", "arguments": "{}"},
    }])

    loop._save_turn(
        session,
        [{"role": "tool", "tool_call_id": "call_big", "name": "echo", "content": "x" * 30_000}],
        skip=0,
    )
    persisted = session.messages[-1]["content"]
    assert len(persisted) <= 1_000 + 32  # max chars + truncation suffix
    assert persisted.endswith("(truncated)")


def test_save_turn_keeps_tool_results_under_limit():
    loop = _mk_loop()
    session = Session(key="test:under-limit")
    session.add_message("assistant", "working", tool_calls=[{
        "id": "call_ok",
        "type": "function",
        "function": {"name": "echo", "arguments": "{}"},
    }])
    content = "x" * 12_000

    loop._save_turn(
        session,
        [{"role": "tool", "tool_call_id": "call_ok", "name": "echo", "content": content}],
        skip=0,
    )
    assert session.messages[-1]["content"] == content


def test_save_turn_placeholder_for_empty_tool_result_blocks():
    loop = _mk_loop()
    session = Session(key="test:empty-blocks")

    loop._save_turn(
        session,
        [
            _assistant_with_tool_calls(["call_empty"]),
            {"role": "tool", "tool_call_id": "call_empty", "name": "echo", "content": []},
        ],
        skip=0,
    )
    assert [m["role"] for m in session.messages] == ["assistant", "tool"]
    assert session.messages[1]["content"] == [
        {"type": "text", "text": "[tool result omitted during persistence]"}
    ]


def test_save_turn_sanitizes_user_list_blocks_and_skips_empty():
    loop = _mk_loop()
    session = Session(key="test:user-blocks")

    loop._save_turn(
        session,
        [
            {"role": "user", "content": []},
            {"role": "user", "content": [{"type": "text", "text": "hello"}]},
            {"role": "assistant", "content": "done"},
        ],
        skip=0,
    )
    assert [m["role"] for m in session.messages] == ["user", "assistant"]
    assert session.messages[0]["content"] == [{"type": "text", "text": "hello"}]


def test_save_turn_stamps_latency_on_last_assistant():
    loop = _mk_loop()
    session = Session(key="test:latency")

    loop._save_turn(
        session,
        [
            {"role": "assistant", "content": "hello", "tool_calls": [{"id": "c1"}]},
            {"role": "assistant", "content": "final answer"},
        ],
        skip=0,
        turn_latency_ms=12_345,
    )
    assert session.messages[-1]["role"] == "assistant"
    assert session.messages[-1]["content"] == "final answer"
    assert session.messages[-1]["latency_ms"] == 12_345


def test_save_turn_skips_before_skip_index():
    loop = _mk_loop()
    session = Session(key="test:skip")

    loop._save_turn(
        session,
        [
            {"role": "system", "content": "identity"},
            {"role": "user", "content": "prompt"},
            {"role": "assistant", "content": ""},        # inside skip window
            {"role": "assistant", "content": "answer"},
        ],
        skip=2,
    )
    # Only messages[skip:] are sanitized and persisted; the empty assistant
    # in that window is still dropped.
    assert [m["role"] for m in session.messages] == ["assistant"]


# ---------------------------------------------------------------------------
# Runtime checkpoint (A5)
# ---------------------------------------------------------------------------


def _checkpoint_payload(
    assistant_message: dict,
    completed: list[dict] | None = None,
    pending: list[dict] | None = None,
) -> dict:
    return {
        "phase": "tools_completed",
        "iteration": 0,
        "model": "mock-model",
        "assistant_message": assistant_message,
        "completed_tool_results": completed or [],
        "pending_tool_calls": pending or [],
    }


def test_set_and_clear_runtime_checkpoint(tmp_path):
    loop = _mk_loop()
    loop.sessions = SessionManager(workspace=str(tmp_path))
    session = loop.sessions.get_or_create("test:set-clear")

    loop._set_runtime_checkpoint(session, {"phase": "awaiting_tools"})
    assert session.metadata[AgentLoop._RUNTIME_CHECKPOINT_KEY] == {"phase": "awaiting_tools"}

    loop.sessions.invalidate(session.key)
    reloaded = loop.sessions.get_or_create("test:set-clear")
    assert reloaded.metadata[AgentLoop._RUNTIME_CHECKPOINT_KEY] == {"phase": "awaiting_tools"}

    loop._clear_runtime_checkpoint(reloaded)
    assert AgentLoop._RUNTIME_CHECKPOINT_KEY not in reloaded.metadata


def test_restore_runtime_checkpoint_rehydrates_completed_and_pending_tools():
    loop = _mk_loop()
    session = Session(
        key="test:checkpoint",
        metadata={
            AgentLoop._RUNTIME_CHECKPOINT_KEY: _checkpoint_payload(
                _assistant_with_tool_calls(["call_done", "call_pending"]),
                completed=[
                    {"role": "tool", "tool_call_id": "call_done", "name": "echo", "content": "ok"},
                ],
                pending=[
                    {"id": "call_pending", "type": "function",
                     "function": {"name": "echo", "arguments": "{}"}},
                ],
            )
        },
    )

    restored = loop._restore_runtime_checkpoint(session)

    assert restored is True
    assert AgentLoop._RUNTIME_CHECKPOINT_KEY not in session.metadata
    assert session.messages[0]["role"] == "assistant"
    assert session.messages[1]["tool_call_id"] == "call_done"
    assert session.messages[2]["tool_call_id"] == "call_pending"
    assert "interrupted before this tool finished" in session.messages[2]["content"].lower()


def test_restore_runtime_checkpoint_dedupes_overlapping_tail():
    loop = _mk_loop()
    session = Session(
        key="test:checkpoint-overlap",
        messages=[
            _assistant_with_tool_calls(["call_done", "call_pending"]),
            {"role": "tool", "tool_call_id": "call_done", "name": "echo", "content": "ok"},
        ],
        metadata={
            AgentLoop._RUNTIME_CHECKPOINT_KEY: _checkpoint_payload(
                _assistant_with_tool_calls(["call_done", "call_pending"]),
                completed=[
                    {"role": "tool", "tool_call_id": "call_done", "name": "echo", "content": "ok"},
                ],
                pending=[
                    {"id": "call_pending", "type": "function",
                     "function": {"name": "echo", "arguments": "{}"}},
                ],
            )
        },
    )

    restored = loop._restore_runtime_checkpoint(session)

    assert restored is True
    assert len(session.messages) == 3
    assert session.messages[0]["role"] == "assistant"
    assert session.messages[1]["tool_call_id"] == "call_done"
    assert session.messages[2]["tool_call_id"] == "call_pending"


def test_restore_runtime_checkpoint_noop_without_checkpoint():
    loop = _mk_loop()
    session = Session(key="test:no-checkpoint")
    session.add_message("user", "hi")

    restored = loop._restore_runtime_checkpoint(session)

    assert restored is False
    assert [m["role"] for m in session.messages] == ["user"]


def test_restore_runtime_checkpoint_clears_pending_user_turn():
    loop = _mk_loop()
    session = Session(
        key="test:checkpoint-clears-pending",
        metadata={
            AgentLoop._PENDING_USER_TURN_KEY: True,
            AgentLoop._RUNTIME_CHECKPOINT_KEY: _checkpoint_payload(
                _assistant_with_tool_calls(["call_pending"]),
                pending=[
                    {"id": "call_pending", "type": "function",
                     "function": {"name": "echo", "arguments": "{}"}},
                ],
            ),
        },
    )

    loop._restore_runtime_checkpoint(session)

    assert AgentLoop._PENDING_USER_TURN_KEY not in session.metadata
    assert AgentLoop._RUNTIME_CHECKPOINT_KEY not in session.metadata


def test_state_restore_materializes_checkpoint(tmp_path):
    loop = _make_full_loop(tmp_path)
    session = loop.sessions.get_or_create("test:state-restore")
    session.add_message("user", "keep progress")
    session.metadata[AgentLoop._RUNTIME_CHECKPOINT_KEY] = _checkpoint_payload(
        _assistant_with_tool_calls(["call_pending"]),
        pending=[
            {"id": "call_pending", "type": "function",
             "function": {"name": "echo", "arguments": "{}"}},
        ],
    )
    loop.sessions.save(session)

    ctx = TurnContext(
        msg=InboundMessage(content="continue here", chat_id="state-restore"),
        session_key="test:state-restore",
    )
    event = asyncio.run(loop._state_restore(ctx))

    assert event == "ok"
    assert [m["role"] for m in ctx.session.messages] == ["user", "assistant", "tool"]
    assert ctx.session.messages[-1]["tool_call_id"] == "call_pending"
    assert AgentLoop._RUNTIME_CHECKPOINT_KEY not in ctx.session.metadata
    assert AgentLoop._PENDING_USER_TURN_KEY not in ctx.session.metadata


def test_full_loop_save_after_restore_does_not_duplicate(tmp_path):
    """Checkpoint restore + a fresh turn must not double-write restored rows."""
    loop = _make_full_loop(tmp_path)
    session = loop.sessions.get_or_create("test:resume")
    session.metadata[AgentLoop._RUNTIME_CHECKPOINT_KEY] = _checkpoint_payload(
        _assistant_with_tool_calls(["call_pending"]),
        pending=[
            {"id": "call_pending", "type": "function",
             "function": {"name": "echo", "arguments": "{}"}},
        ],
    )
    loop.sessions.save(session)

    ctx = TurnContext(
        msg=InboundMessage(content="continue", chat_id="resume"),
        session_key="test:resume",
        turn_wall_started_at=time.time(),
    )
    asyncio.run(loop._state_restore(ctx))
    asyncio.run(loop._state_compact(ctx))

    loop.provider = _ScriptedProvider([
        LLMResponse(content="next answer", finish_reason="stop"),
    ])
    ctx.history = ctx.session.get_history(max_messages=50, max_tokens=loop.replay_budget)
    ctx.initial_messages = loop.context.build_messages(
        current_message="continue",
        history=ctx.history,
        identity=loop.identity,
        session_summary=ctx.pending_summary,
    )
    asyncio.run(loop._state_run(ctx))
    asyncio.run(loop._state_save(ctx))

    roles = [m["role"] for m in ctx.session.messages]
    assert roles == ["assistant", "tool", "assistant"]
    assert AgentLoop._RUNTIME_CHECKPOINT_KEY not in ctx.session.metadata


# ---------------------------------------------------------------------------
# Runner checkpoint emission
# ---------------------------------------------------------------------------


async def _collect_checkpoints(spec_extra: dict | None = None):
    collected: list[dict] = []

    async def _checkpoint(payload: dict) -> None:
        collected.append(payload)

    registry = ToolRegistry()
    registry.register(EchoTool())
    extra = dict(spec_extra or {})
    spec = AgentRunSpec(
        initial_messages=[{"role": "user", "content": "run tools"}],
        tools=registry,
        provider=_ScriptedProvider([]),
        max_iterations=3,
        model="mock-model",
        checkpoint_callback=_checkpoint,
        **extra,
    )
    return spec, collected


@pytest.mark.asyncio
async def test_runner_emits_awaiting_tools_then_tools_completed_then_final():
    spec, collected = await _collect_checkpoints()
    spec.provider = _ScriptedProvider([
        LLMResponse(
            content="",
            tool_calls=[_tool_call("call_1")],
            finish_reason="tool_calls",
        ),
        LLMResponse(content="all done", finish_reason="stop"),
    ])

    result = await AgentRunner().run(spec)

    assert result.final_content == "all done"
    phases = [p["phase"] for p in collected]
    assert phases == ["awaiting_tools", "tools_completed", "final_response"]

    awaiting = collected[0]
    assert awaiting["model"] == "mock-model"
    assert awaiting["pending_tool_calls"] == [{
        "id": "call_1",
        "type": "function",
        "function": {"name": "echo", "arguments": '{"text": "hi"}'},
    }]
    assert awaiting["completed_tool_results"] == []
    assert awaiting["assistant_message"]["tool_calls"][0]["id"] == "call_1"

    completed = collected[1]
    assert completed["pending_tool_calls"] == []
    assert len(completed["completed_tool_results"]) == 1
    assert completed["completed_tool_results"][0]["tool_call_id"] == "call_1"

    final = collected[2]
    assert final["assistant_message"]["content"] == "all done"


@pytest.mark.asyncio
async def test_runner_emits_final_response_checkpoint_on_injection_continue():
    collected: list[dict] = []
    inject_calls: dict[str, int] = {"count": 0}

    async def _checkpoint(payload: dict) -> None:
        collected.append(payload)

    async def _inject(*, limit: int = 3) -> list[dict]:
        if inject_calls["count"] > 0:
            return []
        inject_calls["count"] += 1
        return [{"role": "user", "content": "follow up"}]

    registry = ToolRegistry()
    registry.register(EchoTool())
    spec = AgentRunSpec(
        initial_messages=[{"role": "user", "content": "hello"}],
        tools=registry,
        provider=_ScriptedProvider([
            LLMResponse(content="first", finish_reason="stop"),
            LLMResponse(content="final answer", finish_reason="stop"),
        ]),
        max_iterations=3,
        model="mock-model",
        checkpoint_callback=_checkpoint,
        injection_callback=_inject,
    )

    result = await AgentRunner().run(spec)

    assert result.final_content == "final answer"
    final_phases = [p for p in collected if p["phase"] == "final_response"]
    # One checkpoint when the turn continues via injection, another when the
    # turn actually finalizes; the last snapshot wins for crash recovery.
    assert len(final_phases) == 2
    assert final_phases[0]["assistant_message"]["content"] == "first"
    assert final_phases[0]["iteration"] is not None
    assert final_phases[-1]["assistant_message"]["content"] == "final answer"


@pytest.mark.asyncio
async def test_runner_without_checkpoint_callback_is_noop():
    registry = ToolRegistry()
    registry.register(EchoTool())
    spec = AgentRunSpec(
        initial_messages=[{"role": "user", "content": "hello"}],
        tools=registry,
        provider=_ScriptedProvider([
            LLMResponse(content="done", finish_reason="stop"),
        ]),
        max_iterations=3,
    )

    result = await AgentRunner().run(spec)

    assert result.final_content == "done"


@pytest.mark.asyncio
async def test_process_system_message_restores_checkpoint_and_saves_sanitized(tmp_path):
    loop = _make_full_loop(tmp_path)
    session = loop.sessions.get_or_create("cli:sys-checkpoint")
    session.metadata[AgentLoop._RUNTIME_CHECKPOINT_KEY] = _checkpoint_payload(
        _assistant_with_tool_calls(["call_pending"]),
        pending=[
            {"id": "call_pending", "type": "function",
             "function": {"name": "echo", "arguments": "{}"}},
        ],
    )
    loop.sessions.save(session)

    loop.provider = _ScriptedProvider([
        LLMResponse(content="background ok", finish_reason="stop"),
    ])
    loop.consolidator.maybe_consolidate_by_tokens = AsyncMock(return_value=False)

    outbound = await loop._process_system_message(
        InboundMessage(
            channel="system",
            sender_id="subagent",
            chat_id="cli:sys-checkpoint",
            content="subagent result",
            metadata={"subagent_task_id": "sub-1"},
        ),
    )

    assert outbound is not None
    assert "background ok" in outbound.content
    session = loop.sessions.get_or_create("cli:sys-checkpoint")
    roles = [m["role"] for m in session.messages]
    assert roles == ["assistant", "tool", "assistant", "assistant"]
    assert session.messages[0]["tool_calls"][0]["id"] == "call_pending"
    assert session.messages[1]["tool_call_id"] == "call_pending"
    assert session.messages[2]["injected_event"] == "subagent_result"
    assert session.messages[3]["content"] == "background ok"
    assert AgentLoop._RUNTIME_CHECKPOINT_KEY not in session.metadata
