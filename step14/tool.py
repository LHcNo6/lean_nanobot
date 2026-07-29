from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class ToolResult(str):
    is_error: bool = False

    def __new__(cls, content: str = "", is_error: bool = False) -> ToolResult:
        instance = super().__new__(cls, content)
        instance.is_error = is_error
        return instance

    @classmethod
    def error(cls, content: str) -> ToolResult:
        return cls(content, is_error=True)


class Tool(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        ...

    @property
    @abstractmethod
    def parameters(self) -> dict[str, Any]:
        ...

    async def execute(self, **kwargs: Any) -> ToolResult:
        raise NotImplementedError

    def to_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        return name in self._tools

    def get_definitions(self) -> list[dict[str, Any]]:
        return [tool.to_schema() for tool in self._tools.values()]

    async def execute(self, name: str, **params: Any) -> ToolResult:
        tool = self.get(name)
        if tool is None:
            return ToolResult.error(f"Tool '{name}' not found")
        try:
            return await tool.execute(**params)
        except Exception as exc:
            return ToolResult.error(f"Tool '{name}' error: {exc}")
