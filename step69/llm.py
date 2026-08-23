from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Runtime:
    """遗留可变 Runtime（step21 及早期测试使用，暂保留以向后兼容）。"""

    context_window_tokens: int
    max_tokens: int = 4096
    provider: Any = None
    model: str | None = None


@dataclass(frozen=True)
class GenerationSettings:
    """一次生成的默认参数（对齐 nanobot `providers/base.py`）。"""

    temperature: float = 0.7
    max_tokens: int = 4096
    reasoning_effort: str | None = None


@dataclass(frozen=True, slots=True)
class LLMRuntime:
    """一次执行的不可变运行设置（对齐 nanobot `utils/llm_runtime.py`）。

    provider 本身是有状态的，但所有可变的选择/生成参数都在进入 turn 前
    冻结进这个值；consumer 必须读这里的字段，而非 provider.generation。

    loop 从 `context_window_tokens - generation.max_tokens` 反推 replay budget。
    """

    provider: Any
    model: str
    generation: GenerationSettings
    context_window_tokens: int
    model_preset: str | None = None
    snapshot_signature: tuple[object, ...] | None = None

    @property
    def max_tokens(self) -> int:
        """兼容遗留 Runtime API（consolidation 直接读 runtime.max_tokens）。"""
        return self.generation.max_tokens

    @property
    def temperature(self) -> float:
        return self.generation.temperature

    @classmethod
    def capture(
        cls,
        provider: Any,
        model: str | None,
        *,
        context_window_tokens: int,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        model_preset: str | None = None,
        snapshot_signature: tuple[object, ...] | None = None,
    ) -> "LLMRuntime":
        """从 provider 捕获默认值，冻结为不可变运行设置。"""
        generation = GenerationSettings(
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return cls(
            provider=provider,
            model=model or "",
            generation=generation,
            context_window_tokens=context_window_tokens,
            model_preset=model_preset,
            snapshot_signature=snapshot_signature,
        )


@dataclass(frozen=True)
class ModelPreset:
    """命名模型预设（A1 骨架），step27 由 config 替代。"""

    name: str
    model: str
    provider: str | None = None
    context_window_tokens: int = 8192
    max_tokens: int = 4096
    temperature: float = 0.7

    def to_generation_settings(self) -> GenerationSettings:
        return GenerationSettings(
            temperature=self.temperature,
            max_tokens=self.max_tokens,
        )


def resolve_preset(
    presets: dict[str, ModelPreset],
    name: str,
) -> ModelPreset:
    """按名解析预设，未命中抛 KeyError（对齐 nanobot `normalize_preset_name`）。"""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("model_preset must be a non-empty string")
    name = name.strip()
    if name not in presets:
        raise KeyError(
            f"model_preset {name!r} not found. Available: {', '.join(presets) or '(none)'}"
        )
    return presets[name]


@dataclass
class ToolCallRequest:
    id: str
    name: str
    arguments: Any

    def has_valid_name(self) -> bool:
        """判定该工具调用是否携带可用的（非空字符串）工具名。

        ``name`` 声明为 ``str`` 但运行时不被强制：模型/网关可能发出
        ``name=None`` 或 ``""`` 的退化调用（对齐 nanobot
        ``providers/base.py:ToolCallRequest.has_valid_name``）。这类调用
        无法执行，若被持久化并重放还会让上游拒绝整个请求。
        """
        return isinstance(self.name, str) and bool(self.name)


@dataclass
class LLMResponse:
    content: str | None
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: dict[str, int] = field(default_factory=dict)
    retry_after: float | None = None  # provider 提供的重试等待秒数。
    reasoning_content: str | None = None  # Kimi / DeepSeek-R1 / MiMo 等。
    thinking_blocks: list[dict] | None = None  # Anthropic extended thinking。
    # 结构化错误元数据（finish_reason == "error" 时由 provider 填充，
    # 供重试策略与 runner 的 arrearage 识别使用）。
    error_status_code: int | None = None
    error_kind: str | None = None  # 例如 "timeout" / "connection"。
    error_type: str | None = None  # 语义化类型，如 insufficient_quota。
    error_code: str | None = None  # 语义化代码，如 rate_limit_exceeded。
    error_retry_after_s: float | None = None
    error_should_retry: bool | None = None

    @property
    def has_tool_calls(self) -> bool:
        """响应是否携带工具调用。"""
        return len(self.tool_calls) > 0

    @property
    def should_execute_tools(self) -> bool:
        """仅在 ``finish_reason`` 是工具可用终止时执行工具。

        对齐 nanobot ``providers/base.py:LLMResponse.should_execute_tools``：
        阻断网关在 ``refusal`` / ``content_filter`` / ``error`` 下注入的
        调用（那些终止下工具调用不可信，禁止执行）。
        """
        if not self.has_tool_calls:
            return False
        return self.finish_reason in ("tool_calls", "function_call", "stop")


@dataclass
class RetryConfig:
    max_retries: int = 3
    base_delay: float = 1.0
    max_delay: float = 60.0
    retry_mode: str = "standard"
