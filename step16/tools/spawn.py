from __future__ import annotations

from typing import TYPE_CHECKING, Any

from step16.tool import Tool, ToolResult

if TYPE_CHECKING:
    from step16.subagent import SubagentManager


class SpawnTool(Tool):
    def __init__(self, manager: SubagentManager | None = None):
        self._manager = manager

    @property
    def name(self) -> str:
        return "spawn"

    @property
    def description(self) -> str:
        return (
            "Spawn a subagent to handle a task in the background. "
            "Use this for complex or time-consuming tasks that can run independently. "
            "The subagent will report back when done."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "The task for the subagent to complete"},
                "label": {"type": "string", "description": "Optional short label for the task"},
            },
            "required": ["task"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        if self._manager is None:
            return ToolResult.error("Subagent manager not available.")
        task = kwargs.get("task", "")
        if not task:
            return ToolResult.error("Task must not be empty.")
        label = kwargs.get("label")
        result = await self._manager.spawn(task=task, label=label)
        return ToolResult(result)
