from __future__ import annotations

from typing import TYPE_CHECKING, Any

from step64.context import current_request_context
from step64.schema import StringSchema, tool_parameters_schema
from step64.tool import Tool, ToolResult, tool_parameters

if TYPE_CHECKING:
    from step64.subagent import SubagentManager


@tool_parameters(tool_parameters_schema(
    task=StringSchema("The task for the subagent to complete"),
    label=StringSchema("Optional short label for the task"),
    required=["task"],
))
class SpawnTool(Tool):
    _scopes = {"core"}

    def __init__(self, manager: SubagentManager | None = None):
        self._manager = manager

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        manager = getattr(ctx, "subagent_manager", None)
        return cls(manager=manager)

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

    async def execute(self, task: str = "", label: str | None = None, **kwargs: Any) -> ToolResult:
        if self._manager is None:
            return ToolResult.error("Subagent manager not available.")
        if not task:
            return ToolResult.error("Task must not be empty.")
        req = current_request_context()
        session_key = req.session_key if req else None
        result = await self._manager.spawn(task=task, label=label, session_key=session_key)
        return ToolResult(result)
