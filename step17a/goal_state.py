from __future__ import annotations

import json
from typing import Any, Mapping

GOAL_STATE_KEY = "goal_state"
MAX_GOAL_OBJECTIVE_CHARS = 4000


def parse_goal_state(blob: Any) -> dict[str, Any] | None:
    if blob is None:
        return None
    if isinstance(blob, dict):
        return blob
    if isinstance(blob, str):
        try:
            parsed = json.loads(blob)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def sustained_goal_active(metadata: Mapping[str, Any] | None) -> bool:
    if not metadata:
        return False
    goal = parse_goal_state(metadata.get(GOAL_STATE_KEY))
    return isinstance(goal, dict) and goal.get("status") == "active"


def goal_state_runtime_lines(metadata: Mapping[str, Any] | None) -> list[str]:
    if not metadata:
        return []
    goal = parse_goal_state(metadata.get(GOAL_STATE_KEY))
    if not isinstance(goal, dict) or goal.get("status") != "active":
        return []
    objective = str(goal.get("objective") or "").strip()
    if not objective:
        return ["Goal: active (no objective text stored)."]
    if len(objective) > MAX_GOAL_OBJECTIVE_CHARS:
        objective = objective[:MAX_GOAL_OBJECTIVE_CHARS].rstrip() + "\n… (truncated)"
    out = ["Goal (active):", objective]
    hint = str(goal.get("ui_summary") or "").strip()
    if hint:
        out.append(f"Summary: {hint}")
    return out
