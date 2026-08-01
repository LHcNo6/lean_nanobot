from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Runtime:
    context_window_tokens: int
    max_tokens: int = 4096
    provider: Any = None
    model: str | None = None


@dataclass
class ToolCallRequest:
    id: str
    name: str
    arguments: Any


@dataclass
class LLMResponse:
    content: str | None
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: dict[str, int] = field(default_factory=dict)


@dataclass
class RetryConfig:
    max_retries: int = 3
    base_delay: float = 1.0
    max_delay: float = 60.0
    retry_mode: str = "standard"
