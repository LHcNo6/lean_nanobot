"""Configuration schema using Pydantic.

对齐 nanobot `config/schema.py` 的最小子集（H1）：
- `Base`：camelCase / snake_case 双写兼容（`alias_generator=to_camel` + `populate_by_name`）；
- `ProviderConfig`：一个 LLM provider 的连接配置（api_key / api_base）；
- `ModelPresetConfig`：命名模型预设（对齐 nanobot `model_presets` 表）；
- `AgentDefaults`：agents.defaults 默认参数（workspace / model / provider / 生成参数 /
  fallback_models / max_tool_result_chars / session_ttl_minutes / consolidation_ratio 等）；
- `ChannelsConfig`：通道行为开关 + `extra="allow"` 容纳每通道 section；
- `ProvidersConfig`：内置 provider（对应 step22 registry 六个条目）+ `extra="allow"` 自定义；
- `Config`：根配置；`resolve_preset` / `get_provider` / `get_api_key` / `get_api_base` 等查询方法。

与 nanobot 的差异（刻意简化）：
- 不用 `pydantic_settings.BaseSettings`（env 解析由 `config/loader.py` 手写，见 loader 文档）；
- `fallback_models` 是模型名字符串列表（直接按模型名匹配 provider），
  不做 nanobot 的 "预设名 or 内联配置" 双形态；
- 不含 transcription / gateway / api / tools.* 等未来功能 section（tools 只有一个
  `extra="allow"` 的空 section 用于 `Tool.config_cls()` 落地）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel


class Base(BaseModel):
    """接受 camelCase 与 snake_case 两种键的基类（对齐 nanobot `config_base.Base`）。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ProviderConfig(Base):
    """LLM provider 连接配置。"""

    api_key: str | None = Field(default=None, repr=False)
    api_base: str | None = None
    api_type: Literal["auto", "chat_completions", "responses"] = "auto"


class ModelPresetConfig(Base):
    """命名模型预设（对齐 nanobot `ModelPresetConfig` 子集）。"""

    label: str | None = None
    model: str
    provider: str = "auto"
    max_tokens: int = 8192
    context_window_tokens: int = 200_000
    temperature: float = 0.1

    def to_generation_settings(self) -> Any:
        from step102.llm import GenerationSettings

        return GenerationSettings(
            temperature=self.temperature,
            max_tokens=self.max_tokens,
        )


class DreamConfig(Base):
    """Dream 记忆巩固（step15 演示用，间隔以秒计）。"""

    enabled: bool = True
    interval_seconds: int = 300


class AgentDefaults(Base):
    """agents.defaults — 默认 agent 参数。"""

    workspace: str = "~/.nanobot/workspace"
    model_preset: str | None = None  # 激活的预设名；非 None 时优先于下面散字段
    model: str = "gpt-4o-mini"
    provider: str = "auto"  # provider 配置字段名，或 "auto" 按模型名匹配
    max_tokens: int = 8192
    context_window_tokens: int = 200_000
    temperature: float = 0.1
    fallback_models: list[str] = Field(default_factory=list)  # 模型名字符串列表
    max_tool_iterations: int = 200
    max_concurrent_subagents: int = 5
    max_tool_result_chars: int = Field(default=16_000, ge=0)
    fail_on_tool_error: bool = True
    session_ttl_minutes: int = Field(
        default=0,
        ge=0,
        validation_alias=AliasChoices("sessionTtlMinutes", "session_ttl_minutes"),
        serialization_alias="sessionTtlMinutes",
    )  # 空闲自动压缩阈值（分钟），0 = 关闭
    consolidation_ratio: float = Field(
        default=0.5,
        ge=0.1,
        le=0.95,
        validation_alias=AliasChoices("consolidationRatio", "consolidation_ratio"),
        serialization_alias="consolidationRatio",
    )  # 巩固目标比例（保留预算的百分比）
    disabled_skills: list[str] = Field(default_factory=list)  # 预留（step27 Skills）
    bot_name: str = "lean_nanobot"
    dream: DreamConfig = Field(default_factory=DreamConfig)
    # step29（H8）：所有通道共享一个会话（单用户多设备），对齐 nanobot。
    unified_session: bool = False


class AgentsConfig(Base):
    """agents 配置。"""

    defaults: AgentDefaults = Field(default_factory=AgentDefaults)


class ChannelsConfig(Base):
    """通道行为开关；`extra="allow"` 容纳每通道 section（如 {"cli": {...}}）。"""

    model_config = ConfigDict(extra="allow")

    send_progress: bool = True
    send_tool_hints: bool = False
    show_reasoning: bool = True
    # step64：是否从文档附件提取文本（对齐 nanobot，当前 lean 版仅做开关不实现提取）
    extract_document_text: bool = True
    send_max_retries: int = Field(default=3, ge=0, le=10)

    def channel_sections(self) -> dict[str, dict[str, Any]]:
        """返回每通道配置 section（含自定义 extra），供 ChannelManager 消费。"""
        sections: dict[str, dict[str, Any]] = {}
        for key, value in (self.model_extra or {}).items():
            if isinstance(value, dict):
                sections[key] = value
        return sections


class ProvidersConfig(Base):
    """LLM provider 配置。

    内置 provider 与 step22 `providers/registry.py` 条目一一对应；
    `extra="allow"` 支持自定义 OpenAI 兼容 provider（如 {"my-gateway": {...}}）。
    """

    model_config = ConfigDict(extra="allow")

    custom: ProviderConfig = Field(default_factory=ProviderConfig)  # 用户自供端点
    openai: ProviderConfig = Field(default_factory=ProviderConfig)
    deepseek: ProviderConfig = Field(default_factory=ProviderConfig)
    dashscope: ProviderConfig = Field(default_factory=ProviderConfig)
    openrouter: ProviderConfig = Field(default_factory=ProviderConfig)
    ollama: ProviderConfig = Field(default_factory=ProviderConfig)

    @model_validator(mode="after")
    def convert_extra_providers(self) -> "ProvidersConfig":
        """把 extra 自定义 provider 转成 ProviderConfig，并拒绝与内置重名。"""
        from step102.providers.registry import find_by_name

        if self.model_extra:
            for key, value in self.model_extra.items():
                if find_by_name(key) is not None:
                    raise ValueError(
                        f"providers.{key} conflicts with built-in provider; "
                        "use the built-in provider key or a different custom name"
                    )
                if isinstance(value, dict):
                    self.model_extra[key] = ProviderConfig.model_validate(value)
        return self


class WebToolsConfig(Base):
    """Web 工具配置（step64：对齐 nanobot 最小形态）。"""

    enable: bool = True
    proxy: str | None = None
    user_agent: str | None = None


class ExecToolConfig(Base):
    """Shell exec 工具配置（step64 基础 + step70 增强）。"""

    enable: bool = True
    timeout: int = Field(default=60, ge=0)
    sandbox: str = ""
    # step70 新增：环境变量与命令过滤
    allowed_env_keys: list[str] = Field(default_factory=list)
    allow_patterns: list[str] = Field(default_factory=list)
    deny_patterns: list[str] = Field(default_factory=list)
    path_prepend: str = ""
    path_append: str = ""


class FileToolsConfig(Base):
    """文件系统工具配置（step65：对齐 nanobot filesystem.FileToolsConfig）。"""

    enable: bool = True


class WebToolsConfig(Base):
    """Web 工具配置（step71：网页抓取基础版 + step72：搜索）。"""

    enable: bool = True
    timeout: int = 30
    user_agent: str = "Mozilla/5.0 (learn_nano)"
    search: "WebSearchConfig" = Field(default_factory=lambda: WebSearchConfig())


class WebSearchConfig(Base):
    """Web 搜索配置（step72，step81 增强）。"""

    provider: str = "duckduckgo"
    max_results: int = 5
    timeout: int = 30
    api_key: str = ""  # step81：brave/tavily API key
    base_url: str = ""  # step81：searxng 实例地址


class MyToolConfig(Base):
    """MyTool 运行时自省配置（step75）。"""

    enable: bool = True
    allow_set: bool = False


class ImageGenerationConfig(Base):
    """图片生成工具配置（step79）。"""

    enabled: bool = False
    provider: str = "simple"
    save_dir: str = "generated"  # 内置文件工具默认开启


class ToolsConfig(Base):
    """工具配置 section（`extra="allow"`，按工具名存放，供 `Tool.config_cls()` 消费）。

    对齐 nanobot `ToolsConfig` 的最小形态：nanobot 为每个工具定义类型化子配置
    （web / exec / file / ...），lean 版本以 `{"<config_key>": {...}}` 通用映射替代，
    工具自己的 `config_cls()` 负责把 dict 解析成类型化对象。

    Attributes:
        web: Web 工具配置（step64 新增）。
        exec: Shell exec 工具配置（step64 新增）。
        restrict_to_workspace: 权限意图 —— 默认限制工具文件访问在 workspace 内
            （对齐 nanobot `tools.restrict_to_workspace`，step29 起生效）。
    """

    model_config = ConfigDict(extra="allow")

    web: WebToolsConfig = Field(default_factory=WebToolsConfig)
    exec: ExecToolConfig = Field(default_factory=ExecToolConfig)
    file: FileToolsConfig = Field(default_factory=FileToolsConfig)  # step65 新增
    web: WebToolsConfig = Field(default_factory=WebToolsConfig)  # step71 新增
    my: MyToolConfig = Field(default_factory=MyToolConfig)  # step75 新增
    image_generation: ImageGenerationConfig = Field(default_factory=ImageGenerationConfig)  # step79 新增
    restrict_to_workspace: bool = False


class Config(Base):
    """根配置（对齐 nanobot `Config` 子集）。"""

    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    model_presets: dict[str, ModelPresetConfig] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("modelPresets", "model_presets"),
        serialization_alias="modelPresets",
    )

    @model_validator(mode="after")
    def _validate_model_preset(self) -> "Config":
        if "default" in self.model_presets:
            raise ValueError("model_preset name 'default' is reserved for agents.defaults")
        name = self.agents.defaults.model_preset
        if name and name != "default" and name not in self.model_presets:
            raise ValueError(f"model_preset {name!r} not found in model_presets")
        return self

    # ------------------------------------------------------------------
    # Preset 解析（对齐 nanobot `resolve_preset` 语义）
    # ------------------------------------------------------------------

    def resolve_default_preset(self) -> ModelPresetConfig:
        """从 agents.defaults 字段构造隐式 `default` 预设。"""
        d = self.agents.defaults
        return ModelPresetConfig(
            model=d.model,
            provider=d.provider,
            max_tokens=d.max_tokens,
            context_window_tokens=d.context_window_tokens,
            temperature=d.temperature,
        )

    def resolve_preset(self, name: str | None = None) -> ModelPresetConfig:
        """返回命名预设或隐式 default 预设。

        name 缺省时用 agents.defaults.model_preset；None/""/"default" 走 default 预设。
        """
        if name is None:
            name = self.agents.defaults.model_preset
        if not name or name == "default":
            return self.resolve_default_preset()
        if name not in self.model_presets:
            raise KeyError(f"model_preset {name!r} not found in model_presets")
        return self.model_presets[name]

    @property
    def workspace_path(self) -> Path:
        """展开后的 workspace 路径。"""
        return Path(self.agents.defaults.workspace).expanduser()

    # ------------------------------------------------------------------
    # Provider 匹配（对齐 nanobot `_match_provider` 语义，走 step22 registry）
    # ------------------------------------------------------------------

    def _provider_config_by_name(self, name: str) -> tuple[ProviderConfig | None, str | None]:
        """按 provider 名取配置：(ProviderConfig, spec/自定义名)。"""
        from step102.providers.registry import find_by_name

        normalized = name.strip().lower().replace("-", "_").replace(" ", "_")
        spec = find_by_name(normalized)
        if spec is not None:
            return getattr(self.providers, spec.name, None), spec.name
        for attr_name, value in (self.providers.model_extra or {}).items():
            if attr_name.replace("-", "_").lower() == normalized and isinstance(value, ProviderConfig):
                return value, attr_name
        return None, None

    def _match_provider(
        self, model: str | None = None, *, preset: ModelPresetConfig | None = None
    ) -> tuple[ProviderConfig | None, str | None]:
        """匹配 provider 配置与注册名。返回 (config, spec_name)。"""
        from step102.providers.registry import PROVIDERS, find_by_name, find_by_model

        resolved = preset or self.resolve_preset()
        forced = resolved.provider
        model_lower = (model or resolved.model).strip().lower()

        if forced and forced != "auto":
            return self._provider_config_by_name(forced)

        spec = find_by_model(model_lower)
        if spec is not None:
            return getattr(self.providers, spec.name, None), spec.name
        return None, None

    def get_provider(
        self, model: str | None = None, *, preset: ModelPresetConfig | None = None
    ) -> ProviderConfig | None:
        """返回匹配到的 provider 连接配置（api_key / api_base），未匹配返回 None。"""
        p, _ = self._match_provider(model, preset=preset)
        return p

    def get_provider_name(
        self, model: str | None = None, *, preset: ModelPresetConfig | None = None
    ) -> str | None:
        """返回匹配到的 provider 注册名（如 "deepseek"、"openrouter"）。"""
        _, name = self._match_provider(model, preset=preset)
        return name

    def get_api_key(
        self, model: str | None = None, *, preset: ModelPresetConfig | None = None
    ) -> str | None:
        """返回匹配 provider 的 API key，未配置返回 None（不抛错）。"""
        p = self.get_provider(model, preset=preset)
        return p.api_key if p else None

    def get_api_base(
        self, model: str | None = None, *, preset: ModelPresetConfig | None = None
    ) -> str | None:
        """返回匹配 provider 的 api_base；缺省时回退 spec.default_api_base。"""
        from step102.providers.registry import find_by_name

        p, name = self._match_provider(model, preset=preset)
        if p is not None and p.api_base:
            return p.api_base
        if name:
            spec = find_by_name(name)
            if spec is not None and spec.default_api_base:
                return spec.default_api_base
        return None
