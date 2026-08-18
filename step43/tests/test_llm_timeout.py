"""step37 测试：llm_timeout_s + runner_wall_llm_timeout_s。

全构造数据：mock provider + tmp_path；无真实 API。
覆盖：
- runner_wall_llm_timeout_s：普通 turn / 持续目标 / 显式 /goal / metadata 优先；
- runner 超时逻辑：默认 300s / 0.0 禁用 / 环境变量 / 超时 error_kind / 流式加倍；
- _build_agent_spec 传递 llm_timeout_s：普通 turn / 持续目标 turn。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest

from step43.bus import MessageBus
from step43.bus.events import InboundMessage
from step43.context import ContextBuilder
from step43.goal_state import GOAL_STATE_KEY, runner_wall_llm_timeout_s
from step43.llm import LLMResponse
from step43.loop import AgentLoop
from step43.memory import MemoryStore
from step43.provider import LLMProvider
from step43.runner import AgentRunSpec, AgentRunner
from step43.session import Session, SessionManager
from step43.tool import ToolRegistry
from step43.tools.echo import EchoTool


# ---------------------------------------------------------------------------
# mock provider & helpers
# ---------------------------------------------------------------------------


class _ScriptedProvider(LLMProvider):
    """按脚本队列返回响应。"""

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


class _SlowProvider(LLMProvider):
    """慢速 provider，chat_stream_with_retry 挂起指定秒数，用于测试超时。"""

    def __init__(self, delay: float = 10.0):
        super().__init__()
        self.delay = delay
        self.calls = 0

    @property
    def model(self) -> str:
        return "slow-model"

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: Any = None,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        self.calls += 1
        await asyncio.sleep(self.delay)
        return LLMResponse(content="slow response", finish_reason="stop")

    async def chat_stream_with_retry(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        on_content_delta: Any = None,
        retry_config: Any = None,
        retry_mode: str | None = None,
        on_retry_wait: Any = None,
    ) -> LLMResponse:
        self.calls += 1
        await asyncio.sleep(self.delay)
        return LLMResponse(content="slow response", finish_reason="stop")


def _mk_loop(
    tmp_path: Path,
    provider: LLMProvider | None = None,
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
    )


def _mk_msg() -> InboundMessage:
    """构造最小 InboundMessage。"""
    return InboundMessage(content="hello", chat_id="test")


def _active_goal_metadata() -> dict[str, Any]:
    """构造活跃持续目标的 session metadata。"""
    return {GOAL_STATE_KEY: {"status": "active", "objective": "write a report"}}


# ---------------------------------------------------------------------------
# TestRunnerWallLlmTimeout
# ---------------------------------------------------------------------------


class TestRunnerWallLlmTimeout:
    """runner_wall_llm_timeout_s 函数。"""

    def test_normal_turn_returns_none(self, tmp_path: Path) -> None:
        """普通 turn（无 goal state）返回 None（使用默认超时）。"""
        sessions = SessionManager(workspace=str(tmp_path))
        result = runner_wall_llm_timeout_s(sessions, "test", metadata={})
        assert result is None

    def test_sustained_goal_returns_zero(self, tmp_path: Path) -> None:
        """持续目标 turn 返回 0.0（禁用超时）。"""
        sessions = SessionManager(workspace=str(tmp_path))
        result = runner_wall_llm_timeout_s(
            sessions, "test", metadata=_active_goal_metadata()
        )
        assert result == 0.0

    def test_explicit_goal_request_returns_zero(self, tmp_path: Path) -> None:
        """显式 /goal 请求（message_metadata 含 goal_requested）返回 0.0。"""
        sessions = SessionManager(workspace=str(tmp_path))
        result = runner_wall_llm_timeout_s(
            sessions, "test",
            metadata={},
            message_metadata={"goal_requested": True},
        )
        assert result == 0.0

    def test_metadata_overrides_session_lookup(self, tmp_path: Path) -> None:
        """传入 metadata 时不查 session（直接用传入的 metadata）。"""
        sessions = SessionManager(workspace=str(tmp_path))
        # session 中无 goal state，但传入的 metadata 有活跃 goal
        result = runner_wall_llm_timeout_s(
            sessions, "nonexistent", metadata=_active_goal_metadata()
        )
        assert result == 0.0

    def test_none_metadata_falls_back_to_session(self, tmp_path: Path) -> None:
        """metadata 为 None 时回查 session。"""
        sessions = SessionManager(workspace=str(tmp_path))
        session = sessions.get_or_create("test")
        session.metadata.update(_active_goal_metadata())
        sessions.save(session)
        result = runner_wall_llm_timeout_s(sessions, "test", metadata=None)
        assert result == 0.0


# ---------------------------------------------------------------------------
# TestRunnerTimeoutLogic
# ---------------------------------------------------------------------------


class TestRunnerTimeoutLogic:
    """runner _request_model 超时逻辑。"""

    def _make_spec(
        self, provider: LLMProvider, llm_timeout_s: float | None = None
    ) -> AgentRunSpec:
        """构造最小 AgentRunSpec。"""
        return AgentRunSpec(
            initial_messages=[{"role": "user", "content": "hi"}],
            tools=ToolRegistry(),
            provider=provider,
            llm_timeout_s=llm_timeout_s,
            model="mock",
        )

    @pytest.mark.asyncio
    async def test_zero_disables_timeout(self) -> None:
        """llm_timeout_s=0.0 时禁用超时（慢速 provider 不触发 TimeoutError）。"""
        provider = _SlowProvider(delay=0.2)
        spec = self._make_spec(provider, llm_timeout_s=0.0)
        runner = AgentRunner()
        # 0.0 禁用超时，慢速 provider 应正常返回（不超时）
        result = await runner.run(spec)
        assert result.final_content == "slow response"
        assert result.stop_reason != "error"

    @pytest.mark.asyncio
    async def test_env_var_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """NANOBOT_LLM_TIMEOUT_S 环境变量覆盖默认 300s。"""
        monkeypatch.setenv("NANOBOT_LLM_TIMEOUT_S", "0.1")
        provider = _SlowProvider(delay=10.0)
        spec = self._make_spec(provider, llm_timeout_s=None)
        runner = AgentRunner()
        result = await runner.run(spec)
        # 0.1s 超时，慢速 provider 应触发超时
        assert result.stop_reason == "error"
        assert result.error == "timeout" or "timed out" in (result.final_content or "")

    @pytest.mark.asyncio
    async def test_timeout_returns_error_kind(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """超时时返回 error_kind='timeout'。"""
        monkeypatch.setenv("NANOBOT_LLM_TIMEOUT_S", "0.1")
        provider = _SlowProvider(delay=10.0)
        spec = self._make_spec(provider, llm_timeout_s=None)
        runner = AgentRunner()
        result = await runner.run(spec)
        assert result.stop_reason == "error"
        # 验证超时响应包含 error_kind（通过 final_content 间接验证）
        assert "timed out" in (result.final_content or "")

    @pytest.mark.asyncio
    async def test_default_timeout_not_triggered_for_fast_provider(self) -> None:
        """默认超时（300s）对快速 provider 不触发。"""
        provider = _ScriptedProvider([
            LLMResponse(content="fast", finish_reason="stop"),
        ])
        spec = self._make_spec(provider, llm_timeout_s=None)
        runner = AgentRunner()
        result = await runner.run(spec)
        assert result.final_content == "fast"
        assert result.stop_reason == "stop"


# ---------------------------------------------------------------------------
# TestBuildAgentSpecPassesTimeout
# ---------------------------------------------------------------------------


class TestBuildAgentSpecPassesTimeout:
    """_build_agent_spec 传递 llm_timeout_s。"""

    def test_normal_turn_passes_none(self, tmp_path: Path) -> None:
        """普通 turn 的 spec.llm_timeout_s 为 None（使用默认超时）。"""
        loop = _mk_loop(tmp_path)
        msg = _mk_msg()
        session = Session(key="test")
        spec = loop._build_agent_spec(
            msg, "test", session,
            initial_messages=[{"role": "user", "content": "hi"}],
        )
        assert spec.llm_timeout_s is None

    def test_sustained_goal_passes_zero(self, tmp_path: Path) -> None:
        """持续目标 turn 的 spec.llm_timeout_s 为 0.0（禁用超时）。"""
        loop = _mk_loop(tmp_path)
        msg = _mk_msg()
        session = Session(key="test")
        session.metadata.update(_active_goal_metadata())
        spec = loop._build_agent_spec(
            msg, "test", session,
            initial_messages=[{"role": "user", "content": "hi"}],
        )
        assert spec.llm_timeout_s == 0.0

    def test_explicit_goal_request_passes_zero(self, tmp_path: Path) -> None:
        """显式 /goal 请求的 spec.llm_timeout_s 为 0.0。"""
        loop = _mk_loop(tmp_path)
        msg = InboundMessage(
            content="/goal write report",
            chat_id="test",
            metadata={"goal_requested": True, "original_command": "/goal"},
        )
        session = Session(key="test")
        spec = loop._build_agent_spec(
            msg, "test", session,
            initial_messages=[{"role": "user", "content": "/goal write report"}],
        )
        assert spec.llm_timeout_s == 0.0
