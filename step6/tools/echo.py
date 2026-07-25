from __future__ import annotations

from typing import Any

from step6.tool import Tool, ToolResult


class EchoTool(Tool):
    @property
    def name(self) -> str:
        return "echo"

    @property
    def description(self) -> str:
        return "Echoes back the input text."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "The text to echo back",
                },
            },
            "required": ["text"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        text = kwargs.get("text", "")
        return ToolResult(f"Echo: {text}")
