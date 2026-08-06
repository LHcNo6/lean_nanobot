"""Provider Registry — 单点维护 LLM provider 元数据。

对齐 nanobot `providers/registry.py` 的最小子集：
- `ProviderSpec`：frozen dataclass，一个 provider 的静态元数据；
- `PROVIDERS`：注册表元组，**顺序即匹配优先级**（gateway 在前、标准在后、本地最后）；
- `find_by_name`：按配置字段名（如 "dashscope"）精确查找；
- `find_by_model`：按模型名关键词（如 "gpt-4o" -> openai）子串匹配。

step22 只内置 openai_compat 后端（step21 仅此一个实现），其余后端字段预留。
纯 dataclass，不依赖 pydantic。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ProviderSpec:
    """一个 LLM provider 的静态元数据。

    keywords 为空表示不参与模型名匹配（custom / 动态 provider）。
    is_direct / is_local 的 provider 允许缺失 API key。
    """

    name: str  # 配置字段名，如 "dashscope"
    keywords: tuple[str, ...]  # 模型名关键词（小写）用于 find_by_model
    env_key: str  # API key 的环境变量名
    display_name: str = ""
    backend: str = "openai_compat"
    default_api_base: str = ""  # OpenAI 兼容 base URL
    is_gateway: bool = False  # 任意模型可路由（OpenRouter 等）
    is_local: bool = False  # 本地部署（Ollama 等），无需 key
    is_direct: bool = False  # 用户自供一切（custom），key 可选、api_base 必填
    detect_by_key_prefix: str = ""  # 按 api_key 前缀探测
    detect_by_base_keyword: str = ""  # 按 api_base 子串探测

    @property
    def label(self) -> str:
        return self.display_name or self.name.title()


# ---------------------------------------------------------------------------
# PROVIDERS — 注册表。顺序 = 匹配优先级（gateway 优先，本地最后）。
# ---------------------------------------------------------------------------

PROVIDERS: tuple[ProviderSpec, ...] = (
    # === Custom（用户自供 OpenAI 兼容端点）================
    ProviderSpec(
        name="custom",
        keywords=(),
        env_key="",
        display_name="Custom",
        is_direct=True,
    ),
    # === Gateways（按 key 前缀 / base 关键词探测，可路由任意模型）====
    ProviderSpec(
        name="openrouter",
        keywords=("openrouter",),
        env_key="OPENROUTER_API_KEY",
        display_name="OpenRouter",
        is_gateway=True,
        detect_by_key_prefix="sk-or-",
        detect_by_base_keyword="openrouter",
        default_api_base="https://openrouter.ai/api/v1",
    ),
    # === 标准 provider（按模型名关键词匹配）================
    ProviderSpec(
        name="openai",
        keywords=("openai", "gpt", "o1", "o3", "o4"),
        env_key="OPENAI_API_KEY",
        display_name="OpenAI",
        default_api_base="https://api.openai.com/v1",
    ),
    ProviderSpec(
        name="deepseek",
        keywords=("deepseek",),
        env_key="DEEPSEEK_API_KEY",
        display_name="DeepSeek",
        default_api_base="https://api.deepseek.com",
    ),
    ProviderSpec(
        name="dashscope",
        keywords=("qwen", "dashscope"),
        env_key="DASHSCOPE_API_KEY",
        display_name="DashScope",
        default_api_base="https://dashscope.aliyuncs.com/compatible-mode/v1",
    ),
    # === 本地部署（按配置 key 匹配，不按模型名）============
    ProviderSpec(
        name="ollama",
        keywords=("ollama", "nemotron"),
        env_key="OLLAMA_API_KEY",
        display_name="Ollama",
        is_local=True,
        detect_by_base_keyword="11434",
        default_api_base="http://localhost:11434/v1",
    ),
)


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------


def find_by_name(name: str) -> ProviderSpec | None:
    """按配置字段名查找 spec，如 "dashscope"、"openai"。

    名字归一化：小写、`-` 与空格转 `_`。
    """
    normalized = name.strip().lower().replace("-", "_").replace(" ", "_")
    for spec in PROVIDERS:
        if spec.name == normalized:
            return spec
    return None


def find_by_model(model: str) -> ProviderSpec | None:
    """按模型名关键词匹配 spec（子串匹配，首个命中即返回）。

    注册表顺序即优先级：gateway 关键词命中会先于标准 provider。
    """
    lowered = model.strip().lower()
    if not lowered:
        return None
    for spec in PROVIDERS:
        if any(kw in lowered for kw in spec.keywords):
            return spec
    return None


def create_dynamic_spec(name: str) -> ProviderSpec:
    """为未注册的自定义 provider 名创建动态 spec（is_direct 语义）。"""
    normalized = name.strip().lower().replace("-", "_").replace(" ", "_")
    return ProviderSpec(
        name=normalized,
        keywords=(),
        env_key="",
        display_name=name.title(),
        is_direct=True,
    )
