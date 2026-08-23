from __future__ import annotations

import asyncio
import logging
import random
import re
from abc import ABC, abstractmethod
from contextlib import suppress
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Awaitable, Callable

from step87.llm import LLMResponse, RetryConfig

logger = logging.getLogger(__name__)


class _StreamGuard:
    """流式重试护栏：一旦有 delta 已交付，后续错误不再重试。"""

    delta_delivered: bool = False


def _is_retryable_exception(exc: Exception) -> bool:
    """判定异常是否为可重试的瞬态错误（网络/限流/服务端 5xx）。"""
    if isinstance(exc, asyncio.TimeoutError):
        return True
    try:
        import openai
    except ImportError:
        return False
    if isinstance(exc, (openai.APIConnectionError, openai.APITimeoutError)):
        return True
    if isinstance(exc, openai.RateLimitError):
        return True
    if isinstance(exc, openai.InternalServerError):
        return True
    if isinstance(exc, openai.APIStatusError) and exc.status_code >= 500:
        return True
    return False


def _backoff_delay(attempt: int, config: RetryConfig) -> float:
    """指数退避 + 抖动（attempt 从 0 起）。"""
    delay = min(config.base_delay * (2 ** attempt), config.max_delay)
    delay *= 0.5 + random.random()
    return delay


# 重试心跳分段（对齐 nanobot：长等待也周期性上报 on_retry_wait）。测试会用
# 更小的真实 delay 配合 mock，避免真实等待。
_RETRY_HEARTBEAT_CHUNK = 30.0


class LLMProvider(ABC):
    """LLM provider 基类（对齐 nanobot ``providers/base.py`` 最小集）。

    step32（H5）新增：
    - 响应式重试引擎 ``_run_with_retry``：``finish_reason == "error"`` 的
      响应按 ``is_transient_response`` 分类决定是否重试；
    - ``retry_mode=standard/persistent``：standard 在固定次数后放弃；
      persistent 持续重试（上限 ``_PERSISTENT_MAX_DELAY``，相同错误连续
      ``_PERSISTENT_IDENTICAL_ERROR_LIMIT`` 次后停止）；
    - Retry-After 解析（响应内容 / Retry-After(-ms) 头 / HTTP 日期）；
    - 角色交替强制 ``_enforce_role_alternation``（合并连续同角色消息、
      剥尾条 assistant，防 GLM 1214 类拒绝）。
    """

    _CHAT_RETRY_DELAYS = (1, 2, 4)
    _PERSISTENT_MAX_DELAY = 60.0
    _PERSISTENT_IDENTICAL_ERROR_LIMIT = 10
    _RETRY_HEARTBEAT_CHUNK = 30.0

    _TRANSIENT_ERROR_MARKERS = (
        "429", "rate limit", "500", "502", "503", "504", "overloaded",
        "timeout", "timed out", "connection", "server error",
        "temporarily unavailable", "速率限制", "访问量过大",
    )
    _RETRYABLE_STATUS_CODES = frozenset({408, 409, 429})
    _TRANSIENT_ERROR_KINDS = frozenset({"timeout", "connection"})
    _NON_RETRYABLE_429_ERROR_TOKENS = frozenset({
        "insufficient_quota", "quota_exceeded", "quota_exhausted",
        "billing_hard_limit_reached", "insufficient_balance",
        "credit_balance_too_low", "billing_not_active", "payment_required",
    })
    _RETRYABLE_429_ERROR_TOKENS = frozenset({
        "rate_limit_exceeded", "rate_limit_error", "too_many_requests",
        "request_limit_exceeded", "requests_limit_exceeded", "overloaded_error",
    })
    _NON_RETRYABLE_429_TEXT_MARKERS = (
        "insufficient_quota", "insufficient quota", "quota exceeded",
        "quota exhausted", "billing hard limit", "billing_hard_limit_reached",
        "billing not active", "insufficient balance", "insufficient_balance",
        "credit balance too low", "payment required", "out of credits",
        "out of quota", "exceeded your current quota",
    )
    _RETRYABLE_429_TEXT_MARKERS = (
        "rate limit", "rate_limit", "too many requests", "retry after",
        "try again in", "temporarily unavailable", "overloaded",
        "concurrency limit", "速率限制",
    )

    _SYNTHETIC_USER_CONTENT = "(conversation continued)"

    @abstractmethod
    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        """发送一次非流式 chat 请求，返回 LLMResponse。"""
        ...

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        on_content_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        """流式 chat：默认回退到非流式调用并整体交付单个 delta。

        支持原生流式的 provider 应覆写此方法（对齐 nanobot 基类语义）。
        """
        response = await self.chat(
            messages=messages, tools=tools, model=model,
            temperature=temperature, max_tokens=max_tokens,
        )
        if on_content_delta and response.content:
            await on_content_delta(response.content)
        return response

    # ------------------------------------------------------------------
    # 瞬态 / 欠费分类（H5：transient vs 不可重试）
    # ------------------------------------------------------------------

    @classmethod
    def _normalize_error_token(cls, value: Any) -> str | None:
        """把错误 type/code 归一化为小写 token（None 保持 None）。"""
        if value is None:
            return None
        token = str(value).strip().lower()
        return token or None

    @classmethod
    def _is_transient_error(cls, content: str | None) -> bool:
        """按文本标记判定错误内容是否瞬态（legacy provider 回退路径）。"""
        err = (content or "").lower()
        return any(marker in err for marker in cls._TRANSIENT_ERROR_MARKERS)

    @classmethod
    def is_transient_response(cls, response: LLMResponse) -> bool:
        """响应是否瞬态可重试。

        优先级：结构化 ``error_should_retry`` > 状态码（429 按语义 token /
        文本标记细分，408/409/5xx 重试）> ``error_kind`` > 内容文本标记。
        """
        if response.error_should_retry is not None:
            return bool(response.error_should_retry)
        if response.error_status_code is not None:
            status = int(response.error_status_code)
            if status == 429:
                return cls._is_retryable_429_response(response)
            if status in cls._RETRYABLE_STATUS_CODES or status >= 500:
                return True
        kind = (response.error_kind or "").strip().lower()
        if kind in cls._TRANSIENT_ERROR_KINDS:
            return True
        return cls._is_transient_error(response.content)

    @classmethod
    def is_arrearage_response(cls, response: LLMResponse) -> bool:
        """识别欠费 / 配额 / 计费类错误（重试无法清除）。

        HTTP 402，或 ``insufficient_quota`` / ``payment_required`` 等
        billing 语义 token / 文本标记（对齐 nanobot
        ``LLMProvider.is_arrearage_response``）。
        """
        if response.error_status_code is not None and int(response.error_status_code) == 402:
            return True
        type_token = cls._normalize_error_token(response.error_type)
        code_token = cls._normalize_error_token(response.error_code)
        if any(
            token in cls._NON_RETRYABLE_429_ERROR_TOKENS
            for token in (type_token, code_token)
            if token is not None
        ):
            return True
        content = (response.content or "").lower()
        return any(marker in content for marker in cls._NON_RETRYABLE_429_TEXT_MARKERS)

    @classmethod
    def _is_retryable_429_response(cls, response: LLMResponse) -> bool:
        """429 细分：billing 类不重试；rate-limit 类重试；未知默认重试。"""
        type_token = cls._normalize_error_token(response.error_type)
        code_token = cls._normalize_error_token(response.error_code)
        semantic_tokens = {
            token for token in (type_token, code_token) if token is not None
        }
        if any(token in cls._NON_RETRYABLE_429_ERROR_TOKENS for token in semantic_tokens):
            return False
        content = (response.content or "").lower()
        if any(marker in content for marker in cls._NON_RETRYABLE_429_TEXT_MARKERS):
            return False
        if any(token in cls._RETRYABLE_429_ERROR_TOKENS for token in semantic_tokens):
            return True
        if any(marker in content for marker in cls._RETRYABLE_429_TEXT_MARKERS):
            return True
        return True

    # ------------------------------------------------------------------
    # Retry-After 解析（H5）
    # ------------------------------------------------------------------

    @classmethod
    def _to_retry_seconds(cls, value: float, unit: str | None = None) -> float:
        """把带单位的数值归一化为秒（最小 0.1s）。"""
        normalized_unit = (unit or "s").lower()
        if normalized_unit in {"ms", "milliseconds"}:
            return max(0.1, value / 1000.0)
        if normalized_unit in {"m", "min", "minutes"}:
            return max(0.1, value * 60.0)
        return max(0.1, value)

    @classmethod
    def _extract_retry_after(cls, content: str | None) -> float | None:
        """从错误文本提取 retry-after 秒数（"retry after 5s" / "wait 2s" 等）。"""
        text = (content or "").lower()
        patterns = (
            r"retry after\s+(\d+(?:\.\d+)?)\s*(ms|milliseconds|s|sec|secs|seconds|m|min|minutes)?",
            r"try again in\s+(\d+(?:\.\d+)?)\s*(ms|milliseconds|s|sec|secs|seconds|m|min|minutes)",
            r"wait\s+(\d+(?:\.\d+)?)\s*(ms|milliseconds|s|sec|secs|seconds|m|min|minutes)\s*before retry",
            r"retry[_-]?after[\"'\s:=]+(\d+(?:\.\d+)?)",
        )
        for idx, pattern in enumerate(patterns):
            match = re.search(pattern, text)
            if not match:
                continue
            value = float(match.group(1))
            unit = match.group(2) if idx < 3 else "s"
            return cls._to_retry_seconds(value, unit)
        return None

    @classmethod
    def _extract_retry_after_from_headers(cls, headers: Any) -> float | None:
        """从 HTTP 头提取 retry-after（``Retry-After`` / ``Retry-After-Ms``）。

        数字值按秒；HTTP 日期（``parsedate_to_datetime``）按剩余秒数。
        """
        if not headers:
            return None

        def _header_value(name: str) -> Any:
            if hasattr(headers, "get"):
                value = headers.get(name) or headers.get(name.title())
                if value is not None:
                    return value
            if isinstance(headers, dict):
                for key, value in headers.items():
                    if isinstance(key, str) and key.lower() == name.lower():
                        return value
            return None

        with suppress(TypeError, ValueError):
            retry_ms = _header_value("retry-after-ms")
            if retry_ms is not None:
                value = float(retry_ms) / 1000.0
                if value > 0:
                    return value

        retry_after = _header_value("retry-after")
        if retry_after is None:
            return None
        retry_after_text = str(retry_after).strip()
        if not retry_after_text:
            return None
        if re.fullmatch(r"\d+(?:\.\d+)?", retry_after_text):
            return cls._to_retry_seconds(float(retry_after_text), "s")
        try:
            retry_at = parsedate_to_datetime(retry_after_text)
        except Exception:
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        remaining = (retry_at - datetime.now(retry_at.tzinfo)).total_seconds()
        return max(0.1, remaining)

    @classmethod
    def _extract_retry_after_from_response(cls, response: LLMResponse) -> float | None:
        """从错误响应提取 retry-after：结构化字段 > 文本标记。"""
        if response.error_retry_after_s is not None and response.error_retry_after_s > 0:
            return response.error_retry_after_s
        if response.retry_after is not None and response.retry_after > 0:
            return response.retry_after
        return cls._extract_retry_after(response.content)

    # ------------------------------------------------------------------
    # 角色交替强制（H5：OpenAI-compat / Azure / vLLM / Ollama / GLM 拒绝）
    # ------------------------------------------------------------------

    @staticmethod
    def _enforce_role_alternation(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """合并连续同角色消息、剥掉尾条 assistant、修复首条非 system 为
        assistant 的空洞请求（对齐 nanobot 同名方法）。

        部分 provider 拒绝：末条为 assistant（不支持 prefill）、连续两条
        非 system 消息同角色、system→assistant 开头（GLM error 1214）。
        """
        if not messages:
            return messages

        merged: list[dict[str, Any]] = []
        for msg in messages:
            role = msg.get("role")
            if (
                merged
                and role != "system"
                and role not in ("tool",)
                and merged[-1].get("role") == role
                and role in ("user", "assistant")
            ):
                prev = merged[-1]
                if role == "assistant":
                    prev_has_tools = bool(prev.get("tool_calls"))
                    curr_has_tools = bool(msg.get("tool_calls"))
                    if curr_has_tools:
                        merged[-1] = dict(msg)
                        continue
                    if prev_has_tools:
                        continue
                prev_content = prev.get("content") or ""
                curr_content = msg.get("content") or ""
                if isinstance(prev_content, str) and isinstance(curr_content, str):
                    prev["content"] = (prev_content + "\n\n" + curr_content).strip()
                else:
                    merged[-1] = dict(msg)
            else:
                merged.append(dict(msg))

        last_popped: dict[str, Any] | None = None
        while merged and merged[-1].get("role") == "assistant":
            last_popped = merged.pop()

        # 剥掉尾条 assistant 后只剩 system 的话，把最后一条 assistant 转成
        # user 消息，保证请求对 GLM 等 provider 有效。
        if (
            merged
            and last_popped is not None
            and not any(m.get("role") in ("user", "tool") for m in merged)
        ):
            recovered = dict(last_popped)
            recovered["role"] = "user"
            merged.append(recovered)

        # 安全网：首条非 system 不能是裸 assistant（上游截断可能丢唯一
        # user 消息）；插入一条合成 user 消息保持序列合法。
        for i, msg in enumerate(merged):
            if msg.get("role") != "system":
                if msg.get("role") == "assistant" and not msg.get("tool_calls"):
                    merged.insert(i, {"role": "user", "content": LLMProvider._SYNTHETIC_USER_CONTENT})
                break

        return merged

    # ------------------------------------------------------------------
    # 安全调用包装：异常 → error 响应（重试引擎的统一输入形态）
    # ------------------------------------------------------------------

    async def _safe_chat(self, **kwargs: Any) -> LLMResponse:
        """委托 ``chat``（异常统一由 ``_run_with_retry`` 处理）。"""
        return await self.chat(**kwargs)

    async def _safe_chat_stream(self, **kwargs: Any) -> LLMResponse:
        """委托 ``chat_stream``（异常统一由 ``_run_with_retry`` 处理）。"""
        return await self.chat_stream(**kwargs)

    # ------------------------------------------------------------------
    # 重试引擎（H5：retry_mode=standard/persistent + Retry-After）
    # ------------------------------------------------------------------

    async def chat_with_retry(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        retry_config: RetryConfig | None = None,
        retry_mode: str | None = None,
        on_retry_wait: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        """带重试的非流式 chat。

        ``retry_config`` 控制次数/退避（缺省 ``RetryConfig()``）；
        ``retry_mode`` 覆盖配置（"standard" / "persistent"）。
        """
        config = retry_config or RetryConfig()
        if retry_mode is not None:
            config = RetryConfig(
                max_retries=config.max_retries,
                base_delay=config.base_delay,
                max_delay=config.max_delay,
                retry_mode=retry_mode,
            )
        return await self._run_with_retry(
            self._safe_chat,
            dict(
                messages=messages, tools=tools, model=model,
                temperature=temperature, max_tokens=max_tokens,
            ),
            config=config,
            on_retry_wait=on_retry_wait,
        )

    async def chat_stream_with_retry(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        on_content_delta: Callable[[str], Awaitable[None]] | None = None,
        retry_config: RetryConfig | None = None,
        retry_mode: str | None = None,
        on_retry_wait: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        """带重试的流式 chat（已有 delta 交付后不再重试）。"""
        config = retry_config or RetryConfig()
        if retry_mode is not None:
            config = RetryConfig(
                max_retries=config.max_retries,
                base_delay=config.base_delay,
                max_delay=config.max_delay,
                retry_mode=retry_mode,
            )
        guard = _StreamGuard()

        async def _tracking_delta(text: str) -> None:
            if text:
                guard.delta_delivered = True
            if on_content_delta:
                await on_content_delta(text)

        return await self._run_with_retry(
            self._safe_chat_stream,
            dict(
                messages=messages, tools=tools, model=model,
                temperature=temperature, max_tokens=max_tokens,
                on_content_delta=_tracking_delta,
            ),
            config=config,
            guard=guard,
            on_retry_wait=on_retry_wait,
        )

    async def _sleep_with_heartbeat(
        self,
        delay: float,
        *,
        attempt: int,
        persistent: bool,
        on_retry_wait: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        """按心跳分段 sleep，每段开始前上报重试等待（长等待不静默）。"""
        remaining = max(0.0, delay)
        while remaining > 0:
            if on_retry_wait:
                kind = "persistent retry" if persistent else "retry"
                await on_retry_wait(
                    f"Model request failed, {kind} in {max(1, int(round(remaining)))}s "
                    f"(attempt {attempt})."
                )
            chunk = min(remaining, self._RETRY_HEARTBEAT_CHUNK)
            await asyncio.sleep(chunk)
            remaining -= chunk

    async def _run_with_retry(
        self,
        call: Callable[..., Awaitable[LLMResponse]],
        kw: dict[str, Any],
        *,
        config: RetryConfig,
        guard: _StreamGuard | None = None,
        on_retry_wait: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        """重试主循环（响应式，对齐 nanobot ``_run_with_retry``）。

        - 每次尝试前做角色交替强制（``_enforce_role_alternation``）；
        - 异常：非瞬态（auth/参数错误等）立即上抛；瞬态转成 error 响应
          进入重试引擎，重试耗尽后上抛最后一次异常（保留旧语义）；
        - ``finish_reason != "error"`` 立即返回；
        - error 响应按 ``is_transient_response`` 分类：不可重试直接返回；
        - 流式已交付 delta（``guard.delta_delivered``）不再重试；
        - standard：最多 ``max_retries`` 次重试后放弃（异常上抛 / 错误
          响应原样返回）；
        - persistent：持续重试（延迟上限 ``_PERSISTENT_MAX_DELAY``），
          相同错误连续 ``_PERSISTENT_IDENTICAL_ERROR_LIMIT`` 次后停止；
        - 延迟 = Retry-After（若有）否则指数退避。
        """
        attempt = 0
        persistent = config.retry_mode == "persistent"
        last_response: LLMResponse | None = None
        last_error_key: str | None = None
        identical_error_count = 0
        last_exc: Exception | None = None
        while True:
            attempt += 1
            # H5：请求发出前强制角色交替（对齐 nanobot provider 侧的强制点）。
            if kw.get("messages"):
                kw["messages"] = self._enforce_role_alternation(kw["messages"])
            try:
                response = await call(**kw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not _is_retryable_exception(exc):
                    raise
                if guard is not None and guard.delta_delivered:
                    raise
                last_exc = exc
                response = LLMResponse(
                    content=f"Error calling LLM: {exc}",
                    finish_reason="error",
                    error_kind="timeout" if isinstance(exc, asyncio.TimeoutError) else "exception",
                    error_should_retry=True,
                    error_retry_after_s=self._extract_retry_after(str(exc)),
                )
            if response.finish_reason != "error":
                return response
            if guard is not None and guard.delta_delivered:
                # 流已开始交付内容：失败不能重试（避免用户看到重复内容）。
                return response
            last_response = response

            error_key = (response.content or "").strip().lower() or None
            if error_key and error_key == last_error_key:
                identical_error_count += 1
            else:
                last_error_key = error_key
                identical_error_count = 1 if error_key else 0

            if not self.is_transient_response(response):
                return response

            if persistent and identical_error_count >= self._PERSISTENT_IDENTICAL_ERROR_LIMIT:
                logger.warning(
                    "Stopping persistent retry after %d identical transient errors: %s",
                    identical_error_count, (response.content or "")[:120].lower(),
                )
                if on_retry_wait:
                    await on_retry_wait(
                        f"Persistent retry stopped after {identical_error_count} identical errors."
                    )
                return response

            if not persistent and attempt > config.max_retries:
                logger.warning(
                    "LLM request failed after %d retries, giving up: %s",
                    attempt - 1, (response.content or "")[:120].lower(),
                )
                if last_exc is not None:
                    raise last_exc
                break

            base_delay = _backoff_delay(attempt - 1, config)
            delay = self._extract_retry_after_from_response(response) or base_delay
            if persistent:
                delay = min(delay, self._PERSISTENT_MAX_DELAY)

            logger.warning(
                "LLM transient error (attempt %d/%d), retrying in %.1fs: %s",
                attempt, config.max_retries, delay, (response.content or "")[:120].lower(),
            )
            await self._sleep_with_heartbeat(
                delay, attempt=attempt, persistent=persistent,
                on_retry_wait=on_retry_wait,
            )

        return last_response if last_response is not None else await call(**kw)
