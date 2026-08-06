from __future__ import annotations

from typing import Any

from step23.schema import StringSchema, tool_parameters_schema
from step23.tool import Tool, ToolResult, tool_parameters


@tool_parameters(tool_parameters_schema(
    text=StringSchema("The text to echo back"),
    required=["text"],
))
class EchoTool(Tool):
    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls()

    @property
    def name(self) -> str:
        return "echo"

    @property
    def description(self) -> str:
        return "Echoes back the input text."

    async def execute(self, text: str = "", **kwargs: Any) -> ToolResult:
        return ToolResult(f"Echo: {text}")
