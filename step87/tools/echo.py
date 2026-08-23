from __future__ import annotations

from typing import Any

from pydantic import Field

from step87.config.schema import Base
from step87.schema import StringSchema, tool_parameters_schema
from step87.tool import Tool, ToolResult, tool_parameters


class EchoToolConfig(Base):
    """echo 工具配置（来自 `config.tools.echo`），step27 演示 `Tool.config_cls()`。"""

    enabled: bool = True
    prefix: str = ""
    max_length: int = Field(default=100, ge=1, le=1000)


@tool_parameters(tool_parameters_schema(
    text=StringSchema("The text to echo back"),
    required=["text"],
))
class EchoTool(Tool):
    config_key = "echo"

    def __init__(self, config: EchoToolConfig | None = None) -> None:
        self.tool_config = config or EchoToolConfig()

    @classmethod
    def config_cls(cls) -> type:
        return EchoToolConfig

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        config = cls.resolve_tool_config(ctx)
        return config.enabled if config is not None else True

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls(cls.resolve_tool_config(ctx))

    @property
    def name(self) -> str:
        return "echo"

    @property
    def description(self) -> str:
        return "Echoes back the input text."

    async def execute(self, text: str = "", **kwargs: Any) -> ToolResult:
        content = f"{self.tool_config.prefix}{text}"[: self.tool_config.max_length]
        return ToolResult(f"Echo: {content}")