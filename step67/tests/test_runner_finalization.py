"""step32：runner finalization 对齐测试。

全部使用构造数据 / mock provider，禁止真实 API Key。覆盖：
- max_iterations finalization：无工具请求成功 / error / tool_calls / 关闭开关；
- error / empty 后注入排空：有注入 continue、无注入 return；
- governance 异常保护：prepare_for_model 异常时逐步 repair；
- AgentRunResult 新字段：error / had_injections。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from step67.governance import ContextGovernor
from step67.llm import LLMResponse, ToolCallRequest
from step67.provider import LLMProvider
from step67.runner import (
    AgentRunResult,
    AgentRunSpec,
    AgentRunner,
    _BUDGET_EXHAUSTED_FINALIZATION_PROMPT,
    _EMPTY_FINAL_RESPONSE_MESSAGE,
    _MAX_ITERATIONS_FALLBACK,
)
from step67.tool import ToolRegistry
from step67.tools.echo import EchoTool


# ---------------------------------------------------------------------------
# mock provider & helpers
# ---------------------------------------------------------------------------


class _ScriptedProvider(LLMProvider):
    """按脚本队列返回响应，记录每次调用的 messages 和 tools。"""

    def __init__(self, script: list[LLMResponse]):
        super().__init__()
        self._script = list(script)
        self.calls = 0
        self.call_messages: list[list[dict[str, Any]]] = []
        self.call_tools: list[Any] = []

    @property
    def model(self) -> str:
        return "mock-model"

    async def chat(
        self,
        messages,
        tools=None,
        model=None,
        temperature=0.7,
        max_tokens=4096,
    ):
        self.calls += 1
        self.call_messages.append(list(messages))
        self.call_tools.append(tools)
        if self._script:
            return self._script.pop(0)
        return LLMResponse(content="fallback", finish_reason="stop")


def _tool_call(call_id: str = "call_1", name: str = "echo") -> ToolCallRequest:
    return ToolCallRequest(id=call_id, name=name, arguments={"text": "hi"})


def _make_spec(
    provider: LLMProvider,
    *,
    max_iterations: int = 3,
    injection_callback=None,
    **extra,
) -> AgentRunSpec:
    registry = ToolRegistry()
    registry.register(EchoTool())
    return AgentRunSpec(
        initial_messages=[{"role": "user", "content": "hi"}],
        tools=registry,
        provider=provider,
        model="mock-model",
        max_iterations=max_iterations,
        injection_callback=injection_callback,
        **extra,
    )


def _injection_callback(messages: list[dict[str, Any]]):
    """返回一个一次性注入回调：第一次调用返回给定消息，之后返回空。

    模拟真实 pending queue：取完后队列为空，不再注入。
    """
    state = {"called": False}

    async def _cb() -> list[dict[str, Any]]:
        if state["called"]:
            return []
        state["called"] = True
        return list(messages)

    return _cb


def _empty_injection_callback():
    """返回一个空注入回调（无注入）。"""

    async def _cb() -> list[dict[str, Any]]:
        return []

    return _cb


# ---------------------------------------------------------------------------
# TestMaxIterationsFinalization
# ---------------------------------------------------------------------------


class TestMaxIterationsFinalization:
    @pytest.mark.asyncio
    async def test_finalization_success_uses_model_answer(self):
        """max_iterations 时发无工具请求，模型返回纯文本则用其作为最终答案。"""
        # 前两次返回 tool_calls（消耗迭代），第三次（finalization）返回纯文本。
        provider = _ScriptedProvider([
            LLMResponse(content="", tool_calls=[_tool_call("c1")], finish_reason="tool_calls"),
            LLMResponse(content="", tool_calls=[_tool_call("c2")], finish_reason="tool_calls"),
            LLMResponse(content="Final answer from model.", finish_reason="stop"),
        ])
        result = await AgentRunner().run(_make_spec(provider, max_iterations=2))
        assert result.stop_reason == "max_iterations"
        assert result.final_content == "Final answer from model."
        # finalization 请求不应带工具定义
        assert provider.call_tools[-1] is None
        # 最后一条消息是 assistant 的最终答案
        assert result.messages[-1]["role"] == "assistant"
        assert result.messages[-1]["content"] == "Final answer from model."
        # finalization 提示消息应在 messages 中
        assert any(
            m.get("role") == "user" and _BUDGET_EXHAUSTED_FINALIZATION_PROMPT in str(m.get("content", ""))
            for m in result.messages
        )

    @pytest.mark.asyncio
    async def test_finalization_error_falls_back(self):
        """finalization 请求返回 error 时用 fallback 文案。"""
        provider = _ScriptedProvider([
            LLMResponse(content="", tool_calls=[_tool_call("c1")], finish_reason="tool_calls"),
            LLMResponse(content="", tool_calls=[_tool_call("c2")], finish_reason="tool_calls"),
            LLMResponse(content="", finish_reason="error"),
        ])
        result = await AgentRunner().run(_make_spec(provider, max_iterations=2))
        assert result.stop_reason == "max_iterations"
        assert result.final_content == _MAX_ITERATIONS_FALLBACK

    @pytest.mark.asyncio
    async def test_finalization_tool_calls_falls_back(self):
        """finalization 请求仍返回 tool_calls 时视为失败，用 fallback。"""
        provider = _ScriptedProvider([
            LLMResponse(content="", tool_calls=[_tool_call("c1")], finish_reason="tool_calls"),
            LLMResponse(content="", tool_calls=[_tool_call("c2")], finish_reason="tool_calls"),
            LLMResponse(content="", tool_calls=[_tool_call("c3")], finish_reason="tool_calls"),
        ])
        result = await AgentRunner().run(_make_spec(provider, max_iterations=2))
        assert result.stop_reason == "max_iterations"
        assert result.final_content == _MAX_ITERATIONS_FALLBACK

    @pytest.mark.asyncio
    async def test_finalization_disabled_no_extra_request(self):
        """finalize_on_max_iterations=False 时不发 finalization 请求。"""
        provider = _ScriptedProvider([
            LLMResponse(content="", tool_calls=[_tool_call("c1")], finish_reason="tool_calls"),
            LLMResponse(content="", tool_calls=[_tool_call("c2")], finish_reason="tool_calls"),
        ])
        result = await AgentRunner().run(
            _make_spec(provider, max_iterations=2, finalize_on_max_iterations=False)
        )
        assert result.stop_reason == "max_iterations"
        # 只应有 2 次调用（两次 tool_calls），无 finalization 请求
        assert provider.calls == 2
        # final_content 为 None（隐形续跑接管）
        assert result.final_content is None

    @pytest.mark.asyncio
    async def test_finalization_custom_message(self):
        """自定义 max_iterations_message 在 finalization 失败时使用。"""
        provider = _ScriptedProvider([
            LLMResponse(content="", tool_calls=[_tool_call("c1")], finish_reason="tool_calls"),
            LLMResponse(content="", finish_reason="error"),
        ])
        result = await AgentRunner().run(
            _make_spec(
                provider, max_iterations=1,
                max_iterations_message="Custom budget exhausted message.",
            )
        )
        assert result.final_content == "Custom budget exhausted message."


# ---------------------------------------------------------------------------
# TestErrorInjectionDrain
# ---------------------------------------------------------------------------


class TestErrorInjectionDrain:
    @pytest.mark.asyncio
    async def test_error_with_injection_continues(self):
        """error 后有注入时 continue，不立即返回 error。"""
        # 第一次 error，注入一条 user 消息，第二次正常返回。
        provider = _ScriptedProvider([
            LLMResponse(content="", finish_reason="error"),
            LLMResponse(content="Recovered after injection.", finish_reason="stop"),
        ])
        injection_cb = _injection_callback([
            {"role": "user", "content": "injected follow-up"},
        ])
        result = await AgentRunner().run(
            _make_spec(provider, max_iterations=3, injection_callback=injection_cb)
        )
        # 注入后继续，最终返回正常内容
        assert result.stop_reason == "stop"
        assert result.final_content == "Recovered after injection."
        assert result.had_injections is True
        assert provider.calls == 2

    @pytest.mark.asyncio
    async def test_error_without_injection_returns_error(self):
        """error 后无注入时返回 error 结果。"""
        provider = _ScriptedProvider([
            LLMResponse(content="API failure", finish_reason="error"),
        ])
        result = await AgentRunner().run(
            _make_spec(provider, max_iterations=3, injection_callback=_empty_injection_callback())
        )
        assert result.stop_reason == "error"
        assert result.error is not None
        assert provider.calls == 1


# ---------------------------------------------------------------------------
# TestEmptyInjectionDrain
# ---------------------------------------------------------------------------


class TestEmptyInjectionDrain:
    @pytest.mark.asyncio
    async def test_empty_with_injection_continues(self):
        """空响应重试耗尽后有注入时 continue。"""
        # 前 3 次空响应（2 次重试 + 1 次耗尽），注入后第 4 次正常。
        provider = _ScriptedProvider([
            LLMResponse(content="", finish_reason="stop"),
            LLMResponse(content="", finish_reason="stop"),
            LLMResponse(content="", finish_reason="stop"),
            LLMResponse(content="Answer after injection.", finish_reason="stop"),
        ])
        injection_cb = _injection_callback([
            {"role": "user", "content": "please try again"},
        ])
        result = await AgentRunner().run(
            _make_spec(provider, max_iterations=5, injection_callback=injection_cb)
        )
        # 注入后继续，最终返回正常内容
        assert result.final_content == "Answer after injection."
        assert result.had_injections is True

    @pytest.mark.asyncio
    async def test_empty_without_injection_returns_empty_final(self):
        """空响应重试耗尽且无注入时返回 EMPTY_FINAL_RESPONSE_MESSAGE。"""
        provider = _ScriptedProvider([
            LLMResponse(content="", finish_reason="stop"),
            LLMResponse(content="", finish_reason="stop"),
            LLMResponse(content="", finish_reason="stop"),
        ])
        result = await AgentRunner().run(
            _make_spec(provider, max_iterations=5, injection_callback=_empty_injection_callback())
        )
        assert result.stop_reason == "empty_final_response"
        assert result.final_content == _EMPTY_FINAL_RESPONSE_MESSAGE
        assert result.error == _EMPTY_FINAL_RESPONSE_MESSAGE


# ---------------------------------------------------------------------------
# TestGovernanceFallback
# ---------------------------------------------------------------------------


class _BrokenGovernor(ContextGovernor):
    """故意在 prepare_for_model 抛异常的 governor。"""

    call_count = 0

    def prepare_for_model(self, config, messages, compacted_tool_call_ids=None):
        _BrokenGovernor.call_count += 1
        raise RuntimeError("simulated governance failure")


class TestGovernanceFallback:
    @pytest.mark.asyncio
    async def test_governance_exception_uses_repair(self, monkeypatch):
        """prepare_for_model 抛异常时逐步 repair，不崩溃。"""
        monkeypatch.setattr(
            "step64.runner._GOVERNOR", _BrokenGovernor()
        )
        provider = _ScriptedProvider([
            LLMResponse(content="ok", finish_reason="stop"),
        ])
        # 不应抛异常，应正常返回
        result = await AgentRunner().run(_make_spec(provider, max_iterations=1))
        assert result.final_content == "ok"
        assert _BrokenGovernor.call_count > 0

    @pytest.mark.asyncio
    async def test_governance_repair_preserves_messages(self, monkeypatch):
        """governance 异常后 repair 不丢失原始消息。"""
        monkeypatch.setattr(
            "step64.runner._GOVERNOR", _BrokenGovernor()
        )
        provider = _ScriptedProvider([
            LLMResponse(content="ok", finish_reason="stop"),
        ])
        result = await AgentRunner().run(_make_spec(provider, max_iterations=1))
        # 初始 user 消息应保留
        assert any(m["role"] == "user" and m["content"] == "hi" for m in result.messages)


# ---------------------------------------------------------------------------
# TestAgentRunResultFields
# ---------------------------------------------------------------------------


class TestAgentRunResultFields:
    def test_error_field_default_none(self):
        """error 字段默认为 None。"""
        result = AgentRunResult(final_content="ok", messages=[])
        assert result.error is None

    def test_had_injections_default_false(self):
        """had_injections 字段默认为 False。"""
        result = AgentRunResult(final_content="ok", messages=[])
        assert result.had_injections is False

    @pytest.mark.asyncio
    async def test_error_result_sets_error_field(self):
        """error 终止时 error 字段被设置。"""
        provider = _ScriptedProvider([
            LLMResponse(content="boom", finish_reason="error"),
        ])
        result = await AgentRunner().run(_make_spec(provider, max_iterations=1))
        assert result.stop_reason == "error"
        assert result.error is not None
        assert "boom" in result.error

    @pytest.mark.asyncio
    async def test_max_iterations_had_injections(self):
        """max_iterations 前有注入排空时 had_injections=True。"""
        provider = _ScriptedProvider([
            LLMResponse(content="", tool_calls=[_tool_call("c1")], finish_reason="tool_calls"),
            LLMResponse(content="final", finish_reason="stop"),
        ])
        injection_cb = _injection_callback([
            {"role": "user", "content": "injected"},
        ])
        result = await AgentRunner().run(
            _make_spec(provider, max_iterations=1, injection_callback=injection_cb)
        )
        # max_iterations 边界排空了注入
        assert result.had_injections is True
