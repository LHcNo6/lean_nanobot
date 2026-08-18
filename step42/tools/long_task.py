from __future__ import annotations

from datetime import datetime
from typing import Any

from step42.goal_state import GOAL_STATE_KEY, MAX_GOAL_OBJECTIVE_CHARS, parse_goal_state
from step42.schema import StringSchema, tool_parameters_schema
from step42.tool import Tool, ToolResult, tool_parameters
from step42.session import Session, SessionManager


def _iso_now() -> str:
    return datetime.now().isoformat()


_GOAL_ACTIONS = ("complete", "cancel", "block", "replace")


def _save_goal_state(sess: Session, blob: dict[str, Any]) -> None:
    sess.metadata[GOAL_STATE_KEY] = blob
    sess.updated_at = _iso_now()


@tool_parameters(tool_parameters_schema(
    objective=StringSchema("The sustained objective for this session"),
    ui_summary=StringSchema("Optional one-line display label"),
    required=["objective"],
))
class CreateGoalTool(Tool):
    def __init__(self, sessions: SessionManager | None = None):
        self._sessions = sessions

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        sessions = getattr(ctx, "sessions", None)
        return cls(sessions=sessions)

    @property
    def name(self) -> str:
        return "create_goal"

    @property
    def description(self) -> str:
        return (
            "Create one sustained goal for the current session. "
            "The objective must be self-contained, bounded, safe under repetition, "
            "and explicit about completion criteria."
        )

    async def execute(self, objective: str = "", ui_summary: str | None = None, **kwargs: Any) -> ToolResult:
        from step42.context import current_request_context
        req = current_request_context()
        session_key = req.session_key if req else None
        if self._sessions is None or not session_key:
            return ToolResult.error("Session not available.")
        sess = self._sessions.get_or_create(session_key)
        prior = parse_goal_state(sess.metadata.get(GOAL_STATE_KEY))
        if isinstance(prior, dict) and prior.get("status") == "active":
            return ToolResult.error("A goal is already active. Use update_goal with action='replace' if needed.")
        objective = (objective or "").strip()
        if not objective:
            return ToolResult.error("Objective must not be empty.")
        if len(objective) > MAX_GOAL_OBJECTIVE_CHARS:
            return ToolResult.error(f"Objective too long (max {MAX_GOAL_OBJECTIVE_CHARS} chars).")
        blob = {
            "status": "active",
            "objective": objective,
            "ui_summary": (ui_summary or "").strip()[:120],
            "started_at": _iso_now(),
        }
        _save_goal_state(sess, blob)
        return ToolResult("Goal recorded. Keep working toward the objective. Call update_goal with action='complete' when done.")


@tool_parameters(tool_parameters_schema(
    action=StringSchema("How to update the active goal", enum=list(_GOAL_ACTIONS)),
    recap=StringSchema("Brief honest recap for the user"),
    objective=StringSchema("Replacement objective (required only for 'replace')"),
    ui_summary=StringSchema("Optional one-line display label for replacement"),
    required=["action"],
))
class UpdateGoalTool(Tool):
    def __init__(self, sessions: SessionManager | None = None):
        self._sessions = sessions

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        sessions = getattr(ctx, "sessions", None)
        return cls(sessions=sessions)

    @property
    def name(self) -> str:
        return "update_goal"

    @property
    def description(self) -> str:
        return (
            "Update the active sustained goal. "
            "Use 'complete' when the objective is achieved, 'cancel' when the user cancels, "
            "'block' when progress is blocked, and 'replace' when the objective changes."
        )

    async def execute(self, action: str = "", recap: str | None = None, objective: str | None = None, ui_summary: str | None = None, **kwargs: Any) -> ToolResult:
        from step42.context import current_request_context
        req = current_request_context()
        session_key = req.session_key if req else None
        if self._sessions is None or not session_key:
            return ToolResult.error("Session not available.")
        sess = self._sessions.get_or_create(session_key)
        prior = parse_goal_state(sess.metadata.get(GOAL_STATE_KEY))
        if not isinstance(prior, dict) or prior.get("status") != "active":
            return ToolResult("No active goal to update.")

        normalized = (action or "").strip().lower()
        if normalized not in _GOAL_ACTIONS:
            return ToolResult.error("Action must be one of complete, cancel, block, or replace.")

        if normalized == "replace":
            objective_text = (objective or "").strip()
            if not objective_text:
                return ToolResult.error("Replace action requires a replacement objective.")
            if len(objective_text) > MAX_GOAL_OBJECTIVE_CHARS:
                return ToolResult.error(f"Objective too long (max {MAX_GOAL_OBJECTIVE_CHARS} chars).")
            blob = {
                "status": "active",
                "objective": objective_text,
                "ui_summary": (ui_summary or "").strip()[:120],
                "started_at": _iso_now(),
                "replaced_at": _iso_now(),
                "previous_objective": str(prior.get("objective") or ""),
                "recap": (recap or "").strip(),
            }
            _save_goal_state(sess, blob)
            return ToolResult("Goal replaced. Continue toward the new objective.")

        ended = _iso_now()
        status_map = {"complete": "completed", "cancel": "cancelled", "block": "blocked"}
        blob = {**prior, "status": status_map[normalized], "ended_at": ended, "recap": (recap or "").strip()}
        _save_goal_state(sess, blob)
        tail = (recap or "").strip()
        if tail:
            return ToolResult(f"Goal marked {status_map[normalized]} ({ended}). Recap:\n{tail}")
        return ToolResult(f"Goal marked {status_map[normalized]} ({ended}).")
