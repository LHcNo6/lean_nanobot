"""Provider 层：注册表 / 工厂 / 回退包装。"""

from step94.providers.fallback_provider import FallbackProvider, is_fallbackable_exception
from step94.providers.factory import (
    PROVIDERS,
    ProviderSettings,
    ProviderSnapshot,
    build_provider_snapshot,
    make_provider,
    provider_signature,
)
from step94.providers.registry import (
    ProviderSpec,
    create_dynamic_spec,
    find_by_model,
    find_by_name,
)

__all__ = [
    "PROVIDERS",
    "ProviderSpec",
    "ProviderSettings",
    "ProviderSnapshot",
    "FallbackProvider",
    "find_by_name",
    "find_by_model",
    "create_dynamic_spec",
    "make_provider",
    "build_provider_snapshot",
    "provider_signature",
    "is_fallbackable_exception",
]
