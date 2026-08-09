from __future__ import annotations

import asyncio
import logging
import random
from abc import ABC, abstractmethod
from typing import Any, Awaitable, Callable

from step29.llm import LLMResponse, RetryConfig

logger = logging.getLogger(__name__)


def _is_retryable_exception(exc: Exception) -> bool:
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
    delay = min(config.base_delay * (2 ** attempt), config.max_delay)
    delay *= 0.5 + random.random()
    return delay


# 重试心跳分段（对齐 nanobot：长等待也周期性上报 on_retry_wait）。测试会用
# 更小的真实 delay 配合 mock，避免真实等待。
_RETRY_HEARTBEAT_CHUNK = 30.0


async def _sleep_with_heartbeat(
    delay: float,
    *,
    attempt: int,
    on_retry_wait: Callable[[str], Awaitable[None]] | None = None,
) -> None:
    """按心跳分段 sleep，并在每段开始前上报重试等待消息。

    对齐 nanobot ``providers/base.py:_sleep_with_heartbeat``：对长退避延迟
    让订阅者（如 CLI/WebUI）能感知"正在重试"，而不是长时间静默。
    """
    remaining = max(0.0, delay)
    while remaining > 0:
        if on_retry_wait:
            await on_retry_wait(
                f"Model request failed, retry in {max(1, int(round(remaining)))}s "
                f"(attempt {attempt})."
            )
        chunk = min(remaining, _RETRY_HEARTBEAT_CHUNK)
        await asyncio.sleep(chunk)
        remaining -= chunk


class _StreamGuard:
    delta_delivered: bool = False


class LLMProvider(ABC):
    @abstractmethod
    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> LLMResponse:
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
        response = await self.chat(
            messages=messages, tools=tools, model=model,
            temperature=temperature, max_tokens=max_tokens,
        )
        if on_content_delta and response.content:
            await on_content_delta(response.content)
        return response

    async def chat_with_retry(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        retry_config: RetryConfig | None = None,
        on_retry_wait: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        config = retry_config or RetryConfig()
        attempt = 0
        while True:
            try:
                return await self.chat(
                    messages=messages, tools=tools, model=model,
                    temperature=temperature, max_tokens=max_tokens,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not _is_retryable_exception(exc):
                    raise
                attempt += 1
                if attempt > config.max_retries:
                    raise
                delay = _backoff_delay(attempt - 1, config)
                logger.warning("LLM transient error (attempt %d), retrying in %.1fs: %s", attempt, delay, exc)
                await _sleep_with_heartbeat(
                    delay, attempt=attempt, on_retry_wait=on_retry_wait,
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
        on_retry_wait: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        config = retry_config or RetryConfig()
        guard = _StreamGuard()
        attempt = 0

        async def _tracking_delta(text: str) -> None:
            if text:
                guard.delta_delivered = True
            if on_content_delta:
                await on_content_delta(text)

        while True:
            try:
                return await self.chat_stream(
                    messages=messages, tools=tools, model=model,
                    temperature=temperature, max_tokens=max_tokens,
                    on_content_delta=_tracking_delta,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if guard.delta_delivered:
                    raise
                if not _is_retryable_exception(exc):
                    raise
                attempt += 1
                if attempt > config.max_retries:
                    raise
                delay = _backoff_delay(attempt - 1, config)
                logger.warning("LLM stream transient error (attempt %d), retrying in %.1fs: %s", attempt, delay, exc)
                await _sleep_with_heartbeat(
                    delay, attempt=attempt, on_retry_wait=on_retry_wait,
                )
