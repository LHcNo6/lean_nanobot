"""从配置/设置创建 LLM provider（工厂装配）。

对齐 nanobot `providers/factory.py` 的最小集：
- `ProviderSettings`：纯 dataclass，描述一次装配所需的全部输入（无 config 系统前替代 Config）；
- `make_provider(settings)`：解析 spec → 校验凭据 → 构造 provider；
  有 fallback 列表时包一层 `FallbackProvider`（回退项递归构造但禁用自身回退，防递归）；
- `ProviderSnapshot`：provider + model + context window + signature（为 A1 热刷新预留）；
- `build_provider_snapshot(settings)`：装配快照。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from step24.llm import GenerationSettings
from step24.openai_compat_provider import OpenAICompatProvider
from step24.providers.fallback_provider import FallbackProvider
from step24.providers.registry import (
    PROVIDERS,
    ProviderSpec,
    create_dynamic_spec,
    find_by_model,
    find_by_name,
)


@dataclass
class ProviderSettings:
    """一次 provider 装配的全部输入（无 config 系统时的临时替代）。"""

    model: str
    provider: str | None = None  # 配置字段名，如 "dashscope"；缺省时按模型名关键词匹配
    api_key: str | None = None
    api_base: str | None = None
    temperature: float = 0.7
    max_tokens: int = 4096
    context_window_tokens: int = 8192
    fallbacks: list["ProviderSettings"] = field(default_factory=list)


@dataclass(frozen=True)
class ProviderSnapshot:
    """一次装配的不可变快照；signature 供热刷新检测（step25 后由 config 驱动）。"""

    provider: Any
    model: str
    context_window_tokens: int
    signature: tuple[object, ...]
    generation: GenerationSettings | None = None


def _resolve_spec(settings: ProviderSettings) -> tuple[str, ProviderSpec | None]:
    """解析 provider 名与 spec：显式名 > 模型名关键词 > 动态 custom。"""
    if settings.provider:
        name = settings.provider.strip().lower().replace("-", "_")
        spec = find_by_name(name)
        if spec is None:
            spec = create_dynamic_spec(name)
        return name, spec
    spec = find_by_model(settings.model)
    return (spec.name if spec else None), spec


def _resolve_credentials(
    settings: ProviderSettings,
    provider_name: str | None,
    spec: ProviderSpec | None,
) -> tuple[str, str]:
    """解析 api_key / api_base，未填时从 spec 的 env_key / default_api_base 兜底。"""
    api_key = settings.api_key
    api_base = settings.api_base

    if spec is not None and not api_key and spec.env_key:
        api_key = os.environ.get(spec.env_key) or None
    if spec is not None and not api_base and spec.default_api_base:
        api_base = spec.default_api_base

    key_exempt = spec is not None and (spec.is_local or spec.is_direct)
    if key_exempt:
        if not api_base:
            raise ValueError(
                f"Provider '{spec.name}' requires api_base in settings "
                "(direct/local providers have no built-in endpoint)."
            )
        return api_key or "", api_base

    if not api_key:
        raise ValueError(f"No API key configured for provider '{provider_name or 'unknown'}'.")
    if not api_base:
        raise ValueError(f"No api_base resolved for provider '{provider_name or 'unknown'}'.")
    return api_key, api_base


def _build_provider(settings: ProviderSettings) -> Any:
    """构造一个裸 provider（不回退包装），校验对齐 nanobot factory 语义。"""
    provider_name, spec = _resolve_spec(settings)
    if spec is None and provider_name is None:
        raise ValueError(
            f"cannot resolve provider for model '{settings.model}': "
            "set ProviderSettings.provider or register matching keywords."
        )
    if spec is not None and spec.backend != "openai_compat":
        raise ValueError(
            f"Provider '{spec.name}' backend '{spec.backend}' is not implemented yet."
        )
    api_key, api_base = _resolve_credentials(settings, provider_name, spec)
    return OpenAICompatProvider(
        api_key=api_key,
        api_base=api_base,
        model=settings.model,
    )


def make_provider(settings: ProviderSettings, *, for_fallback: bool = False) -> Any:
    """工厂装配：核心 provider + 可选 FallbackProvider 包装。

    `for_fallback=True` 时忽略自身的 fallbacks（防止递归包装）。
    """
    provider = _build_provider(settings)
    if settings.fallbacks and not for_fallback:
        provider = FallbackProvider(
            primary=provider,
            fallback_presets=list(settings.fallbacks),
            provider_factory=lambda fb: make_provider(fb, for_fallback=True),
        )
    return provider


def provider_signature(settings: ProviderSettings) -> tuple[object, ...]:
    """返回影响装配结果的配置字段签名（供热刷新检测）。"""
    provider_name, spec = _resolve_spec(settings)
    return (
        settings.model,
        provider_name,
        spec.name if spec else None,
        settings.api_key,
        settings.api_base,
        settings.temperature,
        settings.max_tokens,
        settings.context_window_tokens,
        tuple(_provider_signature_fallback(fb) for fb in settings.fallbacks),
    )


def _provider_signature_fallback(settings: ProviderSettings) -> tuple[object, ...]:
    provider_name, spec = _resolve_spec(settings)
    return (
        settings.model,
        provider_name,
        spec.name if spec else None,
        settings.api_key,
        settings.api_base,
        settings.temperature,
        settings.max_tokens,
    )


def build_provider_snapshot(settings: ProviderSettings) -> ProviderSnapshot:
    """装配并返回不可变快照。"""
    return ProviderSnapshot(
        provider=make_provider(settings),
        model=settings.model,
        context_window_tokens=settings.context_window_tokens,
        signature=provider_signature(settings),
        generation=GenerationSettings(
            temperature=settings.temperature,
            max_tokens=settings.max_tokens,
        ),
    )


__all__ = [
    "PROVIDERS",
    "ProviderSettings",
    "ProviderSnapshot",
    "make_provider",
    "build_provider_snapshot",
    "provider_signature",
]
