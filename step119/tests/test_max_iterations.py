"""step36 测试：self.max_iterations 属性 + _sync_subagent_runtime_limits。

全构造数据：mock provider + tmp_path；无真实 API。
覆盖：
- AgentLoop.max_iterations 属性：默认值 / 自定义值；
- _sync_subagent_runtime_limits：同步 subagent / None 安全 / 覆盖默认值；
- _build_agent_spec 使用 self.max_iterations：默认 / 自定义；
- max_iterations 警告日志；
- _run_agent_loop 中调用 sync。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest

from step119.bus import MessageBus
from step119.bus.events import InboundMessage
from step119.context import ContextBuilder
from step119.llm import LLMResponse, ToolCallRequest
from step119.loop import AgentLoop
from step119.memory import MemoryStore
from step119.provider import LLMProvider
from step119.session import Session, SessionManager
from step119.subagent import SubagentManager
from step119.tool import ToolRegistry
from step119.tools.echo import EchoTool


# ---------------------------------------------------------------------------
# mock provider & helpers
# ---------------------------------------------------------------------------


class _ScriptedProvider(LLMProvider):
    """按脚本队列返回响应，记录每次调用。"""

    def __init__(self, script: list[LLMResponse]):
        super().__init__()
        self._script = list(script)
        self.calls = 0

    @property
    def model(self) -> str:
        return "mock-model"

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: Any = None,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        self.calls += 1
        if self._script:
            return self._script.pop(0)
        return LLMResponse(content="fallback", finish_reason="stop")


def _mk_loop(
    tmp_path: Path,
    provider: LLMProvider | None = None,
    *,
    max_iterations: int = 5,
    subagent_manager: SubagentManager | None = None,
) -> AgentLoop:
    """构造最小可运行 AgentLoop。"""
    bus = MessageBus()
    registry = ToolRegistry()
    registry.register(EchoTool())
    return AgentLoop(
        bus=bus,
        provider=provider or _ScriptedProvider([]),
        registry=registry,
        session_manager=SessionManager(workspace=str(tmp_path)),
        context_builder=ContextBuilder(workspace=str(tmp_path)),
        memory=MemoryStore(workspace=str(tmp_path)),
        identity="You are a test bot.",
        replay_budget=10_000,
        max_iterations=max_iterations,
        subagent_manager=subagent_manager,
    )


def _mk_msg() -> InboundMessage:
    """构造最小 InboundMessage。"""
    return InboundMessage(content="hello", chat_id="test")


# ---------------------------------------------------------------------------
# TestMaxIterationsAttribute
# ---------------------------------------------------------------------------


class TestMaxIterationsAttribute:
    """AgentLoop.max_iterations 属性默认值与自定义值。"""

    def test_default_max_iterations_is_5(self, tmp_path: Path) -> None:
        """默认构造时 max_iterations == 5（保持学习版轻量）。"""
        loop = _mk_loop(tmp_path)
        assert loop.max_iterations == 5

    def test_custom_max_iterations(self, tmp_path: Path) -> None:
        """自定义 max_iterations=3 时属性生效。"""
        loop = _mk_loop(tmp_path, max_iterations=3)
        assert loop.max_iterations == 3

    def test_custom_max_iterations_large(self, tmp_path: Path) -> None:
        """自定义较大值（如 50）时属性生效。"""
        loop = _mk_loop(tmp_path, max_iterations=50)
        assert loop.max_iterations == 50


# ---------------------------------------------------------------------------
# TestSyncSubagentRuntimeLimits
# ---------------------------------------------------------------------------


class TestSyncSubagentRuntimeLimits:
    """_sync_subagent_runtime_limits 方法。"""

    def test_syncs_subagent_max_iterations(self, tmp_path: Path) -> None:
        """sync 后 subagent.max_iterations 与 loop.max_iterations 一致。"""
        bus = MessageBus()
        subagents = SubagentManager(bus=bus, max_iterations=10)
        loop = _mk_loop(tmp_path, max_iterations=7, subagent_manager=subagents)
        # sync 前 subagent 是构造时的 10
        assert subagents.max_iterations == 10
        loop._sync_subagent_runtime_limits()
        # sync 后变为 loop 的 7
        assert subagents.max_iterations == 7

    def test_sync_with_none_subagents(self, tmp_path: Path) -> None:
        """subagents 为 None 时调用 sync 不报错。"""
        loop = _mk_loop(tmp_path)
        assert loop.subagents is None
        # 不应抛出异常
        loop._sync_subagent_runtime_limits()

    def test_sync_overrides_subagent_default(self, tmp_path: Path) -> None:
        """subagent 默认 max_iterations=10，sync 后覆盖为 loop 的值。"""
        bus = MessageBus()
        subagents = SubagentManager(bus=bus)  # 默认 max_iterations=10
        loop = _mk_loop(tmp_path, max_iterations=5, subagent_manager=subagents)
        assert subagents.max_iterations == 10
        loop._sync_subagent_runtime_limits()
        assert subagents.max_iterations == 5

    def test_sync_idempotent(self, tmp_path: Path) -> None:
        """多次调用 sync 结果一致。"""
        bus = MessageBus()
        subagents = SubagentManager(bus=bus, max_iterations=10)
        loop = _mk_loop(tmp_path, max_iterations=3, subagent_manager=subagents)
        loop._sync_subagent_runtime_limits()
        assert subagents.max_iterations == 3
        loop._sync_subagent_runtime_limits()
        assert subagents.max_iterations == 3


# ---------------------------------------------------------------------------
# TestBuildAgentSpecUsesMaxIterations
# ---------------------------------------------------------------------------


class TestBuildAgentSpecUsesMaxIterations:
    """_build_agent_spec 中 max_iterations 使用 self.max_iterations。"""

    def test_default_value_in_spec(self, tmp_path: Path) -> None:
        """默认 loop（max_iterations=5）构造的 spec.max_iterations == 5。"""
        loop = _mk_loop(tmp_path)
        msg = _mk_msg()
        session = Session(key="test")
        spec = loop._build_agent_spec(
            channel=msg.channel,
            chat_id=msg.chat_id,
            session_key="test",
            session=session,
            initial_messages=[{"role": "user", "content": "hi"}],
        )
        assert spec.max_iterations == 5

    def test_custom_value_in_spec(self, tmp_path: Path) -> None:
        """自定义 max_iterations=3 的 loop 构造的 spec.max_iterations == 3。"""
        loop = _mk_loop(tmp_path, max_iterations=3)
        msg = _mk_msg()
        session = Session(key="test")
        spec = loop._build_agent_spec(
            channel=msg.channel,
            chat_id=msg.chat_id,
            session_key="test",
            session=session,
            initial_messages=[{"role": "user", "content": "hi"}],
        )
        assert spec.max_iterations == 3

    def test_no_hardcoded_5(self, tmp_path: Path) -> None:
        """验证不再硬编码 5：自定义值 8 时 spec 也是 8。"""
        loop = _mk_loop(tmp_path, max_iterations=8)
        msg = _mk_msg()
        session = Session(key="test")
        spec = loop._build_agent_spec(
            channel=msg.channel,
            chat_id=msg.chat_id,
            session_key="test",
            session=session,
            initial_messages=[{"role": "user", "content": "hi"}],
        )
        assert spec.max_iterations == 8


# ---------------------------------------------------------------------------
# TestMaxIterationsWarning
# ---------------------------------------------------------------------------


class TestMaxIterationsWarning:
    """max_iterations 终止时记录警告日志。"""

    def test_logs_warning_on_max_iterations(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """max_iterations 终止时 logger.warning 被调用，含迭代次数。"""
        # provider 每次都返回 tool_call，触发 max_iterations
        tool_call = ToolCallRequest(
            id="call_1", name="echo", arguments={"text": "hi"},
        )
        provider = _ScriptedProvider([
            LLMResponse(content=None, tool_calls=[tool_call], finish_reason="tool_calls"),
        ] * 10)
        loop = _mk_loop(tmp_path, provider=provider, max_iterations=2)
        msg = _mk_msg()
        session = Session(key="test")

        with caplog.at_level(logging.WARNING, logger="step64.loop"):
            # 传入 pending_queue 使 should_stream_budget_response 返回 False
            # （可续跑时不走 stream 推送），避免触发 effective_stream hook
            # 对象不可调用的已知问题（留待后续 step 修复）。
            asyncio.run(loop._run_agent_loop(
                initial_messages=[{"role": "user", "content": "hi"}],
                channel=msg.channel,
                chat_id=msg.chat_id,
                metadata=msg.metadata if isinstance(msg.metadata, dict) else None,
                original_user_text=msg.content if isinstance(msg.content, str) else None,
                session=session,
                session_key="test",
                runtime=loop.runtime,
                pending_queue=asyncio.Queue(),
            ))

        # 验证有警告日志，含 "Max iterations" 和迭代次数
        warning_msgs = [
            r.message for r in caplog.records
            if r.levelno == logging.WARNING and "Max iterations" in r.message
        ]
        assert len(warning_msgs) >= 1
        assert "2" in warning_msgs[0]  # 含 max_iterations 值


# ---------------------------------------------------------------------------
# TestRunAgentLoopCallsSync
# ---------------------------------------------------------------------------


class TestRunAgentLoopCallsSync:
    """_run_agent_loop 中调用 _sync_subagent_runtime_limits。"""

    def test_run_agent_loop_calls_sync(self, tmp_path: Path) -> None:
        """_run_agent_loop 执行后 subagent.max_iterations 被同步。"""
        bus = MessageBus()
        subagents = SubagentManager(bus=bus, max_iterations=10)
        provider = _ScriptedProvider([
            LLMResponse(content="hello", finish_reason="stop"),
        ])
        loop = _mk_loop(
            tmp_path, provider=provider,
            max_iterations=4, subagent_manager=subagents,
        )
        msg = _mk_msg()
        session = Session(key="test")

        # sync 前 subagent 是构造时的 10
        assert subagents.max_iterations == 10

        asyncio.run(loop._run_agent_loop(
            initial_messages=[{"role": "user", "content": "hi"}],
            channel=msg.channel,
            chat_id=msg.chat_id,
            metadata=msg.metadata if isinstance(msg.metadata, dict) else None,
            original_user_text=msg.content if isinstance(msg.content, str) else None,
            session=session,
            session_key="test",
            runtime=loop.runtime,
        ))

        # _run_agent_loop 内部调用了 sync，subagent 变为 4
        assert subagents.max_iterations == 4

    def test_run_agent_loop_with_none_subagents(self, tmp_path: Path) -> None:
        """subagents 为 None 时 _run_agent_loop 不报错。"""
        provider = _ScriptedProvider([
            LLMResponse(content="hello", finish_reason="stop"),
        ])
        loop = _mk_loop(tmp_path, provider=provider, max_iterations=5)
        assert loop.subagents is None
        msg = _mk_msg()
        session = Session(key="test")

        # 不应抛出异常
        result = asyncio.run(loop._run_agent_loop(
            initial_messages=[{"role": "user", "content": "hi"}],
            channel=msg.channel,
            chat_id=msg.chat_id,
            metadata=msg.metadata if isinstance(msg.metadata, dict) else None,
            original_user_text=msg.content if isinstance(msg.content, str) else None,
            session=session,
            session_key="test",
            runtime=loop.runtime,
        ))
        assert result[3] == "stop"  # stop_reason
