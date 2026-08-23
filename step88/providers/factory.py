"""从配置/设置创建 LLM provider（工厂装配）。

对齐 nanobot `providers/factory.py` 的最小集，**双路分发**：
- **Config 路径（新，step27）**：`make_provider(config, preset_name=...)` —— 从
  `Config.resolve_preset()` 取模型参数，`Config.get_provider/get_provider_name`
  走 registry 匹配 provider，`agents.defaults.fallback_models` 逐级包装
  `FallbackProvider`（对齐 nanobot factory 语义）；
- **ProviderSettings 路径（遗留，step22 起）**：`make_provider(settings)` ——
  dataclass 驱动，保留给旧测试（388 回归零改动）。

`ProviderSnapshot`：provider + model + context window + signature（A1 热刷新预留）。
`provider_signature()`：影响装配结果的字段签名（供热刷新检测）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from step88.llm import GenerationSettings
from step88.openai_compat_provider import OpenAICompatProvider
from step88.providers.fallback_provider import FallbackProvider
from step88.providers.registry import (
    PROVIDERS,
    ProviderSpec,
    create_dynamic_spec,
    find_by_model,
    find_by_name,
)


@dataclass
class ProviderSettings:
    """一次 provider 装配的全部输入（无 config 系统时的临时替代，遗留保留）。"""

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
    """一次装配的不可变快照；signature 供热刷新检测。"""

    provider: Any
    model: str
    context_window_tokens: int
    signature: tuple[object, ...]
    generation: GenerationSettings | None = None


def is_config_input(obj: Any) -> bool:
    """判断装配输入是 Config（新路径）还是 ProviderSettings（遗留路径）。"""
    from step88.config.schema import Config

    return isinstance(obj, Config)


# ===========================================================================
# 遗留路径：ProviderSettings 驱动（step22 起，保持原行为）
# ===========================================================================


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


def _build_provider_from_settings(settings: ProviderSettings) -> Any:
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


def _make_provider_from_settings(
    settings: ProviderSettings, *, for_fallback: bool = False
) -> Any:
    """遗留路径装配：核心 provider + 可选 FallbackProvider 包装。

    `for_fallback=True` 时忽略自身的 fallbacks（防止递归包装）。
    """
    provider = _build_provider_from_settings(settings)
    if settings.fallbacks and not for_fallback:
        provider = FallbackProvider(
            primary=provider,
            fallback_presets=list(settings.fallbacks),
            provider_factory=lambda fb: _make_provider_from_settings(fb, for_fallback=True),
        )
    return provider


def _provider_signature_from_settings(settings: ProviderSettings) -> tuple[object, ...]:
    """遗留路径签名：返回影响装配结果的配置字段签名。"""
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
        tuple(_provider_signature_from_settings(fb) for fb in settings.fallbacks),
    )


def _build_provider_snapshot_from_settings(settings: ProviderSettings) -> ProviderSnapshot:
    """遗留路径：装配并返回不可变快照。"""
    return ProviderSnapshot(
        provider=_make_provider_from_settings(settings),
        model=settings.model,
        context_window_tokens=settings.context_window_tokens,
        signature=_provider_signature_from_settings(settings),
        generation=GenerationSettings(
            temperature=settings.temperature,
            max_tokens=settings.max_tokens,
        ),
    )


# ===========================================================================
# 新路径：Config 驱动（step27，对齐 nanobot factory）
# ===========================================================================


def _resolve_config_preset(config: Any, preset_name: str | None = None) -> Any:
    """解析预设：显式 preset_name 优先，缺省走 agents.defaults.model_preset。"""
    return config.resolve_preset(preset_name)


def _fallback_preset_for(
    primary: Any, model: str
) -> Any:
    """按主预设参数构造 fallback 预设（只换模型名）。"""
    return type(primary)(
        model=model,
        provider=primary.provider,
        max_tokens=primary.max_tokens,
        context_window_tokens=primary.context_window_tokens,
        temperature=primary.temperature,
    )


def _build_provider_from_config(
    config: Any,
    *,
    preset_name: str | None = None,
    preset: Any | None = None,
    model: str | None = None,
) -> Any:
    """构造一个裸 provider（不回退包装），凭据/端点校验对齐 nanobot factory。"""
    resolved = preset if preset is not None else _resolve_config_preset(config, preset_name)
    model = model or resolved.model

    provider_config = config.get_provider(model, preset=resolved)
    provider_name = config.get_provider_name(model, preset=resolved)

    spec = find_by_name(provider_name) if provider_name else None
    if provider_name and spec is None and provider_config is not None:
        if not provider_config.api_base:
            raise ValueError(
                f"Provider '{provider_name}' requires api_base in config "
                "(direct providers have no built-in endpoint)."
            )
        spec = create_dynamic_spec(provider_name)

    if spec is not None and spec.backend != "openai_compat":
        raise ValueError(
            f"Provider '{provider_name}' backend '{spec.backend}' is not implemented yet."
        )
    if spec is None:
        raise ValueError(
            f"cannot resolve provider for model '{model}': "
            "set agents.defaults.provider / model_preset or add a providers.<name> section."
        )

    api_key = provider_config.api_key if provider_config is not None else None
    api_base = provider_config.api_base if provider_config is not None else None
    if not api_key and spec.env_key:
        api_key = os.environ.get(spec.env_key) or None
    if not api_base and spec.default_api_base:
        api_base = spec.default_api_base

    key_exempt = spec.is_local or spec.is_direct
    if key_exempt:
        if not api_base:
            raise ValueError(
                f"Provider '{spec.name}' requires api_base in config "
                "(direct/local providers have no built-in endpoint)."
            )
        api_key = api_key or ""
    else:
        if not api_key:
            raise ValueError(
                f"No API key configured for provider '{provider_name or 'unknown'}'."
            )
        if not api_base:
            raise ValueError(
                f"No api_base resolved for provider '{provider_name or 'unknown'}'."
            )

    return OpenAICompatProvider(
        api_key=api_key,
        api_base=api_base,
        model=model,
    )


def _fallback_models_from_config(config: Any, resolved: Any) -> list[str]:
    """agents.defaults.fallback_models（模型名字符串列表）→ 直接返回。"""
    return list(config.agents.defaults.fallback_models)


def _make_provider_from_config(
    config: Any,
    *,
    preset_name: str | None = None,
    model: str | None = None,
) -> Any:
    """Config 路径装配：核心 provider + fallback_models 逐级 FallbackProvider 包装。"""
    resolved = _resolve_config_preset(config, preset_name)
    provider = _build_provider_from_config(config, preset_name=preset_name, model=model)

    fallback_models = _fallback_models_from_config(config, resolved)
    if fallback_models:
        provider = FallbackProvider(
            primary=provider,
            fallback_presets=[_fallback_preset_for(resolved, m) for m in fallback_models],
            provider_factory=lambda fb: _build_provider_from_config(
                config, preset_name=preset_name, preset=fb
            ),
        )
    return provider


def _provider_signature_from_config(
    config: Any,
    *,
    preset_name: str | None = None,
) -> tuple[object, ...]:
    """Config 路径签名：返回影响装配结果的配置字段。"""
    resolved = _resolve_config_preset(config, preset_name)

    def _fallback_sig(model: str) -> tuple[object, ...]:
        preset = _fallback_preset_for(resolved, model)
        return (
            model,
            preset.provider,
            config.get_provider_name(model, preset=preset),
            config.get_api_key(model, preset=preset),
            config.get_api_base(model, preset=preset),
            preset.max_tokens,
            preset.temperature,
            preset.context_window_tokens,
        )

    return (
        resolved.model,
        resolved.provider,
        config.get_provider_name(resolved.model, preset=resolved),
        config.get_api_key(resolved.model, preset=resolved),
        config.get_api_base(resolved.model, preset=resolved),
        resolved.max_tokens,
        resolved.temperature,
        resolved.context_window_tokens,
        tuple(_fallback_sig(m) for m in _fallback_models_from_config(config, resolved)),
    )


def _build_provider_snapshot_from_config(
    config: Any,
    *,
    preset_name: str | None = None,
) -> ProviderSnapshot:
    """Config 路径：装配并返回不可变快照（context window 取主/回退的最小值）。"""
    resolved = _resolve_config_preset(config, preset_name)
    fallback_windows = [
        _fallback_preset_for(resolved, m).context_window_tokens
        for m in _fallback_models_from_config(config, resolved)
    ]
    return ProviderSnapshot(
        provider=_make_provider_from_config(config, preset_name=preset_name),
        model=resolved.model,
        context_window_tokens=min([resolved.context_window_tokens, *fallback_windows]),
        signature=_provider_signature_from_config(config, preset_name=preset_name),
        generation=resolved.to_generation_settings(),
    )


# ===========================================================================
# 公共入口（双路分发）
# ===========================================================================


def make_provider(
    settings_or_config: Any,
    *,
    preset_name: str | None = None,
    for_fallback: bool = False,
    model: str | None = None,
) -> Any:
    """工厂装配入口。

    - 传 `Config`：走配置路径（`preset_name` 指定预设，`model` 覆盖模型名）；
    - 传 `ProviderSettings`：走遗留路径（`for_fallback` 防递归包装）。
    """
    if is_config_input(settings_or_config):
        return _make_provider_from_config(
            settings_or_config, preset_name=preset_name, model=model
        )
    return _make_provider_from_settings(settings_or_config, for_fallback=for_fallback)


def provider_signature(
    settings_or_config: Any,
    *,
    preset_name: str | None = None,
) -> tuple[object, ...]:
    """返回影响装配结果的配置字段签名（供热刷新检测）。"""
    if is_config_input(settings_or_config):
        return _provider_signature_from_config(settings_or_config, preset_name=preset_name)
    return _provider_signature_from_settings(settings_or_config)


def build_provider_snapshot(
    settings_or_config: Any,
    *,
    preset_name: str | None = None,
) -> ProviderSnapshot:
    """装配并返回不可变快照。"""
    if is_config_input(settings_or_config):
        return _build_provider_snapshot_from_config(
            settings_or_config, preset_name=preset_name
        )
    return _build_provider_snapshot_from_settings(settings_or_config)


__all__ = [
    "PROVIDERS",
    "ProviderSettings",
    "ProviderSnapshot",
    "make_provider",
    "build_provider_snapshot",
    "provider_signature",
    "is_config_input",
]
