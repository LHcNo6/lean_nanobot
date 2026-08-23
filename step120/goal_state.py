from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:
    from step120.session import SessionManager

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


def runner_wall_llm_timeout_s(
    sessions: SessionManager,
    session_key: str | None,
    *,
    metadata: Mapping[str, Any] | None = None,
    message_metadata: Mapping[str, Any] | None = None,
) -> float | None:
    """持续目标 turn 的 LLM 墙钟超时（step37，对齐 nanobot）。

    持续目标（sustained-goal）turn 可能 legitimately 超过默认超时，
    返回 ``0.0`` 表示禁用 ``asyncio.wait_for`` 超时；普通 turn 返回
    ``None``，由 runner 使用环境变量 ``NANOBOT_LLM_TIMEOUT_S``（默认 300s）。

    调用方已持有 session.metadata 时直接传入 ``metadata``，避免重复查库。

    Args:
        sessions: 会话管理器（metadata 为 None 时用于回查）。
        session_key: 会话 key（metadata 为 None 时用于回查）。
        metadata: 本 turn 已持有的会话 metadata（优先使用）。
        message_metadata: 当前消息 metadata（用于判断显式 /goal 请求）。

    Returns:
        ``0.0`` 表示禁用超时；``None`` 表示使用默认超时。
    """
    meta: Mapping[str, Any] | None = metadata
    if meta is None and session_key:
        meta = sessions.get_or_create(session_key).metadata
    return 0.0 if sustained_goal_turn(meta, message_metadata=message_metadata) else None
