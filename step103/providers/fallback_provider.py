"""Provider 包装器：主模型异常时逐级回退到 fallback 模型。

对齐 nanobot `providers/fallback_provider.py` 的最小集，但采用**异常式**触发：
step21 的 LLMResponse 尚无结构化错误字段（error_kind/error_type/...），
因此以「抛出异常」作为回退判定信号，而不是 nanobot 的 `finish_reason == "error"`。

关键设计（对齐 nanobot）：
- 回退是**请求级**的：包装器自身在 turn 之间无状态（仅熔断计数除外）；
- 每个 provider 内部先走自身的 `chat_with_retry` 耗尽重试，仍抛异常才回退；
- 已流式发出内容后失败 → 不再回退（避免重复输出），直接抛出；
- fallback 由 `provider_factory` 按需创建（可防止递归包装）；
- 主模型连续失败触发熔断（3 次 / 60s 冷却，冷却后半开探测）；
- 不可回退异常（认证/权限/400 类）直接抛出，不回退。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from typing import Any, Awaitable, Callable

from step103.llm import LLMResponse, RetryConfig
from step103.provider import _StreamGuard

logger = logging.getLogger(__name__)

# 熔断参数（对齐 nanobot）
_PRIMARY_FAILURE_THRESHOLD = 3
_PRIMARY_COOLDOWN_S = 60.0
_MISSING = object()

_FALLBACKABLE_STATUS_CODES = frozenset({408, 409, 429})
_NON_FALLBACKABLE_STATUS_CODES = frozenset({400, 401, 403, 404, 422})


def is_fallbackable_exception(exc: Exception) -> bool:
    """判定异常是否值得回退（transient 语义）。

    - asyncio.TimeoutError / openai 连接与超时异常 → 回退；
    - 带 status_code 的异常：408/409/429/5xx → 回退；认证/参数类 4xx → 不回退；
    - 其余未知异常 → 不回退（避免掩盖编程错误）。
    """
    if isinstance(exc, asyncio.TimeoutError):
        return True
    try:
        import openai  # type: ignore
    except ImportError:
        openai = None
    if openai is not None:
        if isinstance(exc, (openai.APIConnectionError, openai.APITimeoutError)):
            return True
        if isinstance(exc, openai.APIStatusError):
            return exc.status_code in _FALLBACKABLE_STATUS_CODES or 500 <= exc.status_code <= 599
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        if status in _NON_FALLBACKABLE_STATUS_CODES:
            return False
        return status in _FALLBACKABLE_STATUS_CODES or 500 <= status <= 599
    return False


class FallbackProvider:
    """把主 provider 包一层，出错时按顺序尝试 fallback provider。

    实现 `chat` / `chat_stream` / `chat_with_retry` / `chat_stream_with_retry`
    四个入口（runner 与 consolidation 都直接调用 provider），共享 `_try_chain`。
    """

    def __init__(
        self,
        primary,
        fallback_presets: list[Any],
        provider_factory: Callable[[Any], Any],
    ) -> None:
        self._primary = primary
        self._fallback_presets = list(fallback_presets)
        self._provider_factory = provider_factory
        self._has_fallbacks = bool(fallback_presets)
        self._primary_failures = 0
        self._primary_tripped_at: float | None = None

    @property
    def model(self) -> str:
        return self._primary.model

    @property
    def generation(self) -> Any:
        return getattr(self._primary, "generation", None)

    # ------------------------------------------------------------------
    # 四个入口
    # ------------------------------------------------------------------

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        if not self._has_fallbacks:
            return await self._primary.chat(
                messages=messages, tools=tools, model=model,
                temperature=temperature, max_tokens=max_tokens,
            )
        return await self._try_chain(
            "chat",
            dict(messages=messages, tools=tools, model=model,
                 temperature=temperature, max_tokens=max_tokens),
        )

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        on_content_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        if not self._has_fallbacks:
            return await self._primary.chat_stream(
                messages=messages, tools=tools, model=model,
                temperature=temperature, max_tokens=max_tokens,
                on_content_delta=on_content_delta,
            )
        return await self._try_chain(
            "chat_stream",
            dict(messages=messages, tools=tools, model=model,
                 temperature=temperature, max_tokens=max_tokens,
                 on_content_delta=on_content_delta),
        )

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
        if not self._has_fallbacks:
            return await self._primary.chat_with_retry(
                messages=messages, tools=tools, model=model,
                temperature=temperature, max_tokens=max_tokens,
                retry_config=retry_config,
                on_retry_wait=on_retry_wait,
            )
        return await self._try_chain(
            "chat_with_retry",
            dict(messages=messages, tools=tools, model=model,
                 temperature=temperature, max_tokens=max_tokens,
                 retry_config=retry_config,
                 on_retry_wait=on_retry_wait),
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
        if not self._has_fallbacks:
            return await self._primary.chat_stream_with_retry(
                messages=messages, tools=tools, model=model,
                temperature=temperature, max_tokens=max_tokens,
                on_content_delta=on_content_delta,
                retry_config=retry_config,
                on_retry_wait=on_retry_wait,
            )
        guard = _StreamGuard()

        async def _tracking_delta(text: str) -> None:
            if text:
                guard.delta_delivered = True
            if on_content_delta:
                await on_content_delta(text)

        return await self._try_chain(
            "chat_stream_with_retry",
            dict(messages=messages, tools=tools, model=model,
                 temperature=temperature, max_tokens=max_tokens,
                 on_content_delta=_tracking_delta,
                 retry_config=retry_config,
                 on_retry_wait=on_retry_wait),
            guard=guard,
        )

    # ------------------------------------------------------------------
    # 熔断
    # ------------------------------------------------------------------

    def _primary_available(self) -> bool:
        """主 provider 未被熔断（冷却期外 / 半开探测）。"""
        if self._primary_tripped_at is None:
            return True
        return time.monotonic() - self._primary_tripped_at >= _PRIMARY_COOLDOWN_S

    def _record_primary_failure(self, message: str) -> None:
        self._primary_failures += 1
        if self._primary_failures >= _PRIMARY_FAILURE_THRESHOLD:
            self._primary_tripped_at = time.monotonic()
            logger.warning(
                "primary model circuit open after %d consecutive failures",
                self._primary_failures,
            )

    def _record_primary_success(self) -> None:
        self._primary_failures = 0
        self._primary_tripped_at = None

    # ------------------------------------------------------------------
    # 核心回退链
    # ------------------------------------------------------------------

    @staticmethod
    def _filter_kwargs(method: Callable[..., Any], kwargs: dict[str, Any]) -> dict[str, Any]:
        """按目标方法的签名过滤 kwargs（对齐 runner 的签名探测风格）。

        mock provider（回归测试）与真实 provider 的签名宽度不一致：把新增的
        ``on_retry_wait`` 等参数原样透传给不接受它的 mock 会 TypeErorr，
        因此在目标方法无 ``**kwargs`` 时只保留签名中出现的参数。
        """
        try:
            signature = inspect.signature(method)
        except (TypeError, ValueError):
            return kwargs
        if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values()):
            return kwargs
        allowed = {p.name for p in signature.parameters.values()}
        return {k: v for k, v in kwargs.items() if k in allowed}

    async def _try_chain(
        self,
        method: str,
        kwargs: dict[str, Any],
        guard: _StreamGuard | None = None,
    ) -> LLMResponse:
        primary_model = kwargs.get("model") or self._primary.model
        primary_attempted = False
        last_exc: Exception | None = None

        if self._primary_available():
            primary_attempted = True
            try:
                response = await getattr(self._primary, method)(
                    **self._filter_kwargs(getattr(self._primary, method), kwargs)
                )
                self._record_primary_success()
                return response
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if guard is not None and guard.delta_delivered:
                    logger.warning(
                        "primary streamed content before failing; skipping failover: %s", exc
                    )
                    raise
                if not is_fallbackable_exception(exc):
                    raise
                last_exc = exc
                self._record_primary_failure(str(exc))
                logger.warning("primary model failed, trying fallbacks: %s", exc)
        else:
            logger.debug("primary model circuit open; skipping primary")

        for idx, preset in enumerate(self._fallback_presets):
            try:
                fallback_provider = self._provider_factory(preset)
            except Exception as exc:
                logger.warning("failed to create fallback provider: %s", exc)
                continue

            original = {
                name: kwargs.get(name, _MISSING)
                for name in ("model", "max_tokens", "temperature")
            }
            kwargs["model"] = preset.model
            kwargs["max_tokens"] = preset.max_tokens
            kwargs["temperature"] = preset.temperature
            try:
                try:
                    response = await getattr(fallback_provider, method)(
                        **self._filter_kwargs(
                            getattr(fallback_provider, method), kwargs
                        )
                    )
                    logger.info(
                        "fallback '%s' succeeded (primary '%s' failed)",
                        preset.model, primary_model,
                    )
                    return response
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if guard is not None and guard.delta_delivered:
                        raise
                    if not is_fallbackable_exception(exc):
                        raise
                    last_exc = exc
                    logger.warning(
                        "fallback '%s' also failed: %s", preset.model, exc
                    )
            finally:
                for name, value in original.items():
                    if value is _MISSING:
                        kwargs.pop(name, None)
                    else:
                        kwargs[name] = value

        if primary_attempted and last_exc is not None:
            logger.warning("all fallbacks failed; re-raising primary error")
            raise last_exc
        raise RuntimeError(
            f"primary model '{primary_model}' circuit open and all fallbacks failed"
        )
