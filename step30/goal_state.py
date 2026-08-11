from __future__ import annotations

import json
from typing import Any, Mapping

GOAL_STATE_KEY = "goal_state"
GOAL_COMMAND = "/goal"
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


def explicit_goal_requested(message_metadata: Mapping[str, Any] | None) -> bool:
    """本轮是否由 ``/goal`` 显式发起（对齐 nanobot goal_state 语义）。

    Args:
        message_metadata: 当前消息的 metadata（可空）。

    Returns:
        True 表示消息元数据声明了 goal 请求（``goal_requested`` 标记或
        原始命令为 ``/goal``）。
    """
    if not message_metadata:
        return False
    if message_metadata.get("goal_requested") is True:
        return True
    return str(message_metadata.get("original_command") or "").strip() == GOAL_COMMAND


def sustained_goal_turn(
    metadata: Mapping[str, Any] | None,
    *,
    message_metadata: Mapping[str, Any] | None = None,
) -> bool:
    """本轮是否应使用 sustained-goal 语义（活跃 goal 或显式 /goal 请求）。

    Args:
        metadata: 会话 metadata。
        message_metadata: 当前消息 metadata（可空）。

    Returns:
        True 表示活跃持续目标或 /goal 发起轮。
    """
    return sustained_goal_active(metadata) or explicit_goal_requested(message_metadata)


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
        objective = objective[:MAX_GOAL_OBJECTIVE_CHARS].rstrip() + "\n(truncated)"
    out = ["Goal (active):", objective]
    hint = str(goal.get("ui_summary") or "").strip()
    if hint:
        out.append(f"Summary: {hint}")
    return out
