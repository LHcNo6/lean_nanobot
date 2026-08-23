"""Step 30 A8/H5 测试：runner 健壮性 + provider 重试引擎。

全部使用构造数据 / mock provider，禁止真实 API Key。覆盖：
- runner：refusal/content_filter 下丢弃工具调用、欠费识别换文案、
  自定义 error_message / max_iterations_message、usage 缺失估算回退；
- provider：Retry-After 解析（文本/头/HTTP 日期）、429 quota 不重试 /
  rate-limit 重试、persistent 模式（超过 standard 上限仍重试、相同错误
  上限后停止）、不可重试错误直接返回、角色交替强制。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from step69.llm import LLMResponse, RetryConfig, ToolCallRequest
from step69.provider import LLMProvider
from step69.runner import AgentRunSpec, AgentRunner
from step69.tool import ToolRegistry
from step69.tools.echo import EchoTool


# ---------------------------------------------------------------------------
# mock provider
# ---------------------------------------------------------------------------


class _ScriptedProvider(LLMProvider):
    """按脚本队列返回响应，计数调用次数。"""

    def __init__(self, script: list[LLMResponse]):
        super().__init__()
        self._script = list(script)
        self.calls = 0

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
        if self._script:
            return self._script.pop(0)
        return LLMResponse(content="fallback", finish_reason="stop")


class _ClassMethodProbe(LLMProvider):
    """仅用于调用基类 classmethod/staticmethod（具体子类，绕过 ABC 实例化检查）。"""

    async def chat(self, messages, tools=None, model=None, temperature=0.7, max_tokens=4096):
        return LLMResponse(content="", finish_reason="stop")


def _tool_call(call_id: str = "call_1") -> ToolCallRequest:
    return ToolCallRequest(id=call_id, name="echo", arguments={"text": "hi"})


def _make_spec(provider: LLMProvider, **extra) -> AgentRunSpec:
    registry = ToolRegistry()
    registry.register(EchoTool())
    return AgentRunSpec(
        initial_messages=[{"role": "user", "content": "hi"}],
        tools=registry,
        provider=provider,
        model="mock-model",
        **extra,
    )


# ---------------------------------------------------------------------------
# A8：runner 健壮性
# ---------------------------------------------------------------------------


class TestRunnerRobustness:
    @pytest.mark.asyncio
    async def test_refusal_tool_calls_discarded(self):
        """refusal 终止下的工具调用不可信：不执行，文本作为最终内容。"""
        provider = _ScriptedProvider([
            LLMResponse(
                content="I cannot do that.",
                tool_calls=[_tool_call()],
                finish_reason="refusal",
            ),
        ])
        result = await AgentRunner().run(_make_spec(provider))
        assert result.final_content == "I cannot do that."
        assert result.tools_used == []
        # 不应出现 tool 消息（工具从未执行）
        assert all(m["role"] != "tool" for m in result.messages)

    @pytest.mark.asyncio
    async def test_arrearage_error_uses_arrearage_message(self):
        """HTTP 402 / billing 错误 → 欠费文案，而非原始错误文本。"""
        provider = _ScriptedProvider([
            LLMResponse(
                content="insufficient_quota: out of credits",
                finish_reason="error",
                error_status_code=402,
            ),
        ])
        result = await AgentRunner().run(_make_spec(provider))
        assert result.stop_reason == "error"
        assert "API key in arrears" in result.final_content

    @pytest.mark.asyncio
    async def test_custom_error_message_wins(self):
        """显式 error_message 优先于欠费识别与响应原文。"""
        provider = _ScriptedProvider([
            LLMResponse(content="boom", finish_reason="error"),
        ])
        result = await AgentRunner().run(
            _make_spec(provider, error_message="custom error text")
        )
        assert result.final_content == "custom error text"

    @pytest.mark.asyncio
    async def test_custom_max_iterations_message(self):
        """max_iterations 收尾文案可定制。"""
        provider = _ScriptedProvider([
            LLMResponse(content="", tool_calls=[_tool_call()], finish_reason="tool_calls"),
        ] * 5)
        result = await AgentRunner().run(
            _make_spec(provider, max_iterations=2, max_iterations_message="budget out")
        )
        assert result.stop_reason == "max_iterations"
        assert result.final_content == "budget out"

    @pytest.mark.asyncio
    async def test_usage_estimated_when_missing(self):
        """provider 不给 usage 时按文本长度估算，usage 簿记不断档。"""
        provider = _ScriptedProvider([
            LLMResponse(content="short answer", finish_reason="stop", usage={}),
        ])
        result = await AgentRunner().run(_make_spec(provider))
        assert result.usage["prompt_tokens"] > 0
        assert result.usage["completion_tokens"] > 0


# ---------------------------------------------------------------------------
# H5：provider 重试引擎
# ---------------------------------------------------------------------------


class _TransientThenSuccessProvider(LLMProvider):
    """前 *fail_count* 次返回瞬态 error 响应，之后成功。"""

    def __init__(self, fail_count: int = 1):
        super().__init__()
        self.fail_count = fail_count
        self.calls = 0

    @property
    def model(self) -> str:
        return "mock-flaky"

    async def chat(
        self,
        messages,
        tools=None,
        model=None,
        temperature=0.7,
        max_tokens=4096,
    ):
        self.calls += 1
        if self.calls <= self.fail_count:
            return LLMResponse(
                content="server error, retry after 0.01s",
                finish_reason="error",
                error_should_retry=True,
            )
        return LLMResponse(content="recovered", finish_reason="stop")


class TestRetryAfterParsing:
    def test_extract_retry_after_text_units(self):
        provider = _ClassMethodProbe()
        assert provider._extract_retry_after("retry after 5s") == 5.0
        assert provider._extract_retry_after("retry after 500ms") == 0.5
        assert provider._extract_retry_after("try again in 2 minutes") == 120.0
        assert provider._extract_retry_after("wait 1.5 sec before retry") == 1.5
        assert provider._extract_retry_after("retry_after=7") == 7.0
        assert provider._extract_retry_after("all good") is None

    def test_extract_retry_after_numeric_header(self):
        provider = _ClassMethodProbe()
        assert provider._extract_retry_after_from_headers({"Retry-After": "30"}) == 30.0
        assert provider._extract_retry_after_from_headers({"retry-after-ms": "1500"}) == 1.5
        assert provider._extract_retry_after_from_headers(None) is None

    def test_extract_retry_after_http_date_header(self):
        provider = _ClassMethodProbe()
        later = datetime.now(timezone.utc) + timedelta(seconds=10)
        http_date = later.strftime("%a, %d %b %Y %H:%M:%S GMT")
        parsed = provider._extract_retry_after_from_headers({"Retry-After": http_date})
        assert parsed is not None and 5 <= parsed <= 15

    def test_extract_retry_after_from_response_priority(self):
        provider = _ClassMethodProbe()
        response = LLMResponse(
            content="server error", finish_reason="error",
            error_retry_after_s=8.0, retry_after=3.0,
        )
        assert provider._extract_retry_after_from_response(response) == 8.0


class TestRetryClassification:
    @pytest.mark.asyncio
    async def test_quota_429_not_retried(self):
        provider = _ScriptedProvider([])
        provider._script_responses = [
            LLMResponse(
                content="insufficient_quota: billing issue",
                finish_reason="error",
                error_status_code=429,
                error_type="insufficient_quota",
            ),
            LLMResponse(content="should never happen", finish_reason="stop"),
        ]

        async def _chat(**kwargs):
            return provider._script_responses.pop(0)

        provider.chat = _chat
        response = await provider.chat_with_retry(
            initial_messages=[{"role": "user", "content": "hi"}],
            retry_config=RetryConfig(max_retries=3, base_delay=0.001, max_delay=0.002),
        )
        # quota 类 429 不重试：直接返回错误响应，未消费第二条
        assert response.finish_reason == "error"
        assert len(provider._script_responses) == 1

    @pytest.mark.asyncio
    async def test_rate_limit_429_retried(self):
        provider = _ScriptedProvider([])
        provider._script_responses = [
            LLMResponse(
                content="rate limit reached",
                finish_reason="error",
                error_status_code=429,
                error_type="rate_limit_exceeded",
            ),
            LLMResponse(content="recovered", finish_reason="stop"),
        ]

        async def _chat(**kwargs):
            return provider._script_responses.pop(0)

        provider.chat = _chat
        response = await provider.chat_with_retry(
            initial_messages=[{"role": "user", "content": "hi"}],
            retry_config=RetryConfig(max_retries=3, base_delay=0.001, max_delay=0.002),
        )
        assert response.content == "recovered"

    def test_is_arrearage_response(self):
        provider = _ClassMethodProbe()
        assert provider.is_arrearage_response(LLMResponse(
            content="", finish_reason="error", error_status_code=402,
        ))
        assert provider.is_arrearage_response(LLMResponse(
            content="", finish_reason="error", error_type="payment_required",
        ))
        assert not provider.is_arrearage_response(LLMResponse(
            content="rate limit", finish_reason="error",
            error_status_code=429, error_type="rate_limit_exceeded",
        ))


class TestRetryModes:
    @pytest.mark.asyncio
    async def test_standard_gives_up_after_max_retries(self):
        provider = _TransientThenSuccessProvider(fail_count=10)
        response = await provider.chat_with_retry(
            initial_messages=[{"role": "user", "content": "hi"}],
            retry_config=RetryConfig(max_retries=2, base_delay=0.001, max_delay=0.002),
        )
        assert response.finish_reason == "error"
        assert provider.calls == 3  # 1 次初始 + 2 次重试

    @pytest.mark.asyncio
    async def test_persistent_retries_beyond_standard(self):
        provider = _TransientThenSuccessProvider(fail_count=4)
        response = await provider.chat_with_retry(
            initial_messages=[{"role": "user", "content": "hi"}],
            retry_config=RetryConfig(
                max_retries=2, base_delay=0.001, max_delay=0.002, retry_mode="persistent",
            ),
        )
        # standard 会在 3 次失败后放弃；persistent 坚持到第 5 次成功
        assert response.content == "recovered"
        assert provider.calls == 5

    @pytest.mark.asyncio
    async def test_persistent_stops_on_identical_errors(self):
        provider = _TransientThenSuccessProvider(fail_count=100)
        response = await provider.chat_with_retry(
            initial_messages=[{"role": "user", "content": "hi"}],
            retry_config=RetryConfig(
                max_retries=2, base_delay=0.001, max_delay=0.002, retry_mode="persistent",
            ),
        )
        assert response.finish_reason == "error"
        assert provider.calls == 10  # _PERSISTENT_IDENTICAL_ERROR_LIMIT

    @pytest.mark.asyncio
    async def test_non_transient_error_returned_without_retry(self):
        provider = _ScriptedProvider([])
        provider._script_responses = [
            LLMResponse(content="invalid api key", finish_reason="error"),
            LLMResponse(content="should never happen", finish_reason="stop"),
        ]

        async def _chat(**kwargs):
            return provider._script_responses.pop(0)

        provider.chat = _chat
        response = await provider.chat_with_retry(
            initial_messages=[{"role": "user", "content": "hi"}],
            retry_config=RetryConfig(max_retries=3, base_delay=0.001, max_delay=0.002),
        )
        assert response.finish_reason == "error"
        assert response.content == "invalid api key"
        assert len(provider._script_responses) == 1  # 未重试

    @pytest.mark.asyncio
    async def test_retry_after_honored_over_backoff(self):
        """错误文本里的 retry-after 优先于退避（此处为 0.01s 快速测试）。"""
        provider = _TransientThenSuccessProvider(fail_count=1)
        started = asyncio.get_event_loop().time()
        response = await provider.chat_with_retry(
            initial_messages=[{"role": "user", "content": "hi"}],
            retry_config=RetryConfig(max_retries=3, base_delay=60.0, max_delay=120.0),
        )
        elapsed = asyncio.get_event_loop().time() - started
        assert response.content == "recovered"
        # 若忽略 retry-after 会等 60s；0.01s 让总耗时 < 5s
        assert elapsed < 5.0


class TestRoleAlternation:
    def test_merge_consecutive_user_messages(self):
        provider = _ClassMethodProbe()
        merged = provider._enforce_role_alternation([
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "a"},
            {"role": "user", "content": "b"},
            {"role": "assistant", "content": "ok"},
        ])
        # 连续 user 合并；尾条 assistant 被剥掉（不支持 prefill）
        roles = [m["role"] for m in merged]
        assert roles == ["system", "user"]
        assert merged[1]["content"] == "a\n\nb"

    def test_drop_trailing_assistant(self):
        provider = _ClassMethodProbe()
        merged = provider._enforce_role_alternation([
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "prefill"},
        ])
        assert [m["role"] for m in merged] == ["user"]

    def test_system_only_after_drop_recovers_as_user(self):
        provider = _ClassMethodProbe()
        merged = provider._enforce_role_alternation([
            {"role": "system", "content": "sys"},
            {"role": "assistant", "content": "orphan"},
        ])
        roles = [m["role"] for m in merged]
        assert roles == ["system", "user"]

    def test_first_non_system_assistant_gets_synthetic_user(self):
        provider = _ClassMethodProbe()
        merged = provider._enforce_role_alternation([
            {"role": "system", "content": "sys"},
            {"role": "assistant", "content": "bare"},
            {"role": "user", "content": "question"},
        ])
        # 首条非 system 是裸 assistant：插入合成 user 保持序列合法（GLM 1214）
        roles = [m["role"] for m in merged]
        assert roles == ["system", "user", "assistant", "user"]
        assert "conversation continued" in merged[1]["content"]
