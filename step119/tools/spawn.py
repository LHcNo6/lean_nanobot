from __future__ import annotations

from typing import TYPE_CHECKING, Any

from step119.context import current_request_context
from step119.schema import StringSchema, tool_parameters_schema
from step119.security.workspace_access import current_workspace_scope
from step119.tool import Tool, ToolResult, tool_parameters

if TYPE_CHECKING:
    from step119.subagent import SubagentManager


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
        # 捕获父 turn 的请求上下文与 workspace 范围，透传给子代理，
        # 使子代理在「父会话的上下文」中执行（对齐 nanobot 的 origin 透传）。
        req = current_request_context()
        origin = {
            "channel": req.channel if req else "cli",
            "chat_id": req.chat_id if req else "direct",
            "session_key": req.session_key if req else None,
            "message_id": req.message_id if req else None,
            "runtime": req.runtime if req else None,
            "workspace_scope": current_workspace_scope(),
        }
        result = await self._manager.spawn(task=task, label=label, origin=origin)
        return ToolResult(result)
