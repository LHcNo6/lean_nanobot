from __future__ import annotations

from typing import TYPE_CHECKING, Any

from step125.context import current_request_context
from step125.schema import NumberSchema, StringSchema, tool_parameters_schema
from step125.security.workspace_access import current_workspace_scope
from step125.tool import Tool, ToolResult, tool_parameters

if TYPE_CHECKING:
    from step125.subagent import SubagentManager


@tool_parameters(tool_parameters_schema(
    task=StringSchema("The task for the subagent to complete"),
    label=StringSchema("Optional short label for the task"),
    temperature=NumberSchema(
        0.7,
        description="Optional generation temperature override for the subagent (0.0=deterministic, 2.0=max creative)",
        minimum=0.0,
        maximum=2.0,
    ),
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

    async def execute(self, task: str = "", label: str | None = None, temperature: float | None = None, **kwargs: Any) -> ToolResult:
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
            # step123：对齐 nanobot，透传父消息 id 供 announce 路由到原消息
            "origin_message_id": req.message_id if req else None,
            "runtime": req.runtime if req else None,
            "workspace_scope": current_workspace_scope(),
        }
        # step125（G7）：temperature 覆写透传给 manager.spawn（None 时等同不覆写）。
        result = await self._manager.spawn(
            task=task, label=label, origin=origin, temperature=temperature
        )
        return ToolResult(result)
