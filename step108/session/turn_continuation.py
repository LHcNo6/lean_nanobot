"""内部 turn 续跑策略（对齐 nanobot ``session/turn_continuation.py``）。

A12：sustained goal 到达 LLM 预算边界（max_iterations）时，本轮不做
用户可见收尾，而是通过 pending_queue 排班一条"隐形续跑"消息
（``sender_id="system:continuation"``），由 ``_dispatch`` finally 的
re-publish 回流总线，作为**新的 turn** 重新走完整状态机。续跑片：
- 不持久化系统性 user 输入（``should_persist_user_message``）；
- 不产出收到 fallback 文案（``finalize_on_max_iterations=False`` →
  loop 把最终合成 assistant 消息从历史中剥掉）；
- 跨片传播可见运行起点（``visible_run_started_at``），turn latency
  统计全程而非单片；
- 会话级轮次计数器 ``_sustained_goal_continuation_rounds`` 上限
  ``_MAX_GOAL_CONTINUATION_ROUNDS = 12``，避免无限续跑。

模块刻意不引用 AgentLoop：loop 只调用少量辅助函数（策略外置），
保证可单测。``_save_skip_for_turn`` 供 ``_state_save`` 计算持久化
append 边界，替换硬编码 skip。
"""

from __future__ import annotations

import dataclasses
from typing import Any, Mapping, MutableMapping

from step108.goal_state import (
    goal_state_runtime_lines,
    sustained_goal_active,
    sustained_goal_turn,
)

INTERNAL_CONTINUATION_META = "_internal_continuation"
INTERNAL_CONTINUATION_KIND_META = "_internal_continuation_kind"
INTERNAL_CONTINUATION_PENDING_META = "_internal_continuation_pending"
INTERNAL_CONTINUATION_RUN_STARTED_AT_META = "_internal_continuation_run_started_at"
SKIP_USER_PERSIST_META = "_skip_user_persist"

_GOAL_CONTINUATION_KIND = "sustained_goal"
_GOAL_CONTINUATION_SENDER = "system:continuation"
_GOAL_CONTINUATION_ROUNDS_KEY = "_sustained_goal_continuation_rounds"
_MAX_GOAL_CONTINUATION_ROUNDS = 12
_STRIPPED_INBOUND_META_KEYS = {
    INTERNAL_CONTINUATION_PENDING_META,
    "goal_requested",
    "original_command",
}


def internal_continuation_inbound(metadata: Mapping[str, Any] | None) -> bool:
    """判定入站消息是否由内部续跑策略产生。

    Args:
        metadata: 消息 metadata（InboundMessage.metadata）。

    Returns:
        True 表示这是隐形续跑片（非用户真实输入）。
    """
    return bool(metadata and metadata.get(INTERNAL_CONTINUATION_META) is True)


def internal_continuation_pending(metadata: Mapping[str, Any] | None) -> bool:
    """判定当前消息的 turn 是否已排班隐形续跑片。

    Args:
        metadata: 消息 metadata。

    Returns:
        True 表示本轮已把续跑消息排进 pending queue（后续片接管
        用户可见响应）。
    """
    return bool(metadata and metadata.get(INTERNAL_CONTINUATION_PENDING_META) is True)


def internal_continuation_run_started_at(metadata: Mapping[str, Any] | None) -> float | None:
    """取跨续跑片传播的可见运行起点（供 latency 统计全程）。

    Args:
        metadata: 消息 metadata。

    Returns:
        起点时间戳（秒）；缺失或非法时返回 None。
    """
    if not metadata:
        return None
    value = metadata.get(INTERNAL_CONTINUATION_RUN_STARTED_AT_META)
    if not isinstance(value, int | float):
        return None
    started_at = float(value)
    return started_at if started_at > 0 else None


def should_persist_user_message(metadata: Mapping[str, Any] | None) -> bool:
    """判断该入站消息是否应持久化为用户输入。

    Args:
        metadata: 消息 metadata。

    Returns:
        False 表示跳过持久化（``_skip_user_persist`` 标记，或续跑消息本身）。
    """
    if metadata and metadata.get(SKIP_USER_PERSIST_META) is True:
        return False
    return not internal_continuation_inbound(metadata)


def should_finalize_on_max_iterations(
    *,
    pending_queue_available: bool,
    session_metadata: Mapping[str, Any] | None,
    message_metadata: Mapping[str, Any] | None = None,
) -> bool:
    """max_iterations 边界是否应产出最终响应（供 runner 决定收尾）。

    预算边界上若还能内部续跑（有 pending queue 且 goal 续跑可用），
    本轮不需要额外收尾——用户可见最终响应由后续续跑片产出。

    Args:
        pending_queue_available: 本 turn 是否持有 pending queue（可排班续跑）。
        session_metadata: 会话 metadata。
        message_metadata: 当前消息 metadata（可空）。

    Returns:
        True 表示应产出收尾响应；False 表示续跑接管。
    """
    return not (
        pending_queue_available
        and _goal_continuation_available(
            session_metadata,
            message_metadata=message_metadata,
        )
    )


def should_stream_budget_response(
    *,
    stop_reason: str,
    pending_queue_available: bool,
    session_metadata: Mapping[str, Any] | None = None,
    message_metadata: Mapping[str, Any] | None = None,
) -> bool:
    """max_iterations 边界是否应通过 stream 推送最终响应。

    step35：对齐 nanobot ``_run_agent_loop`` 中的 max_iterations 处理。
    当 runner 因 max_iterations 终止时，如果有 stream 回调且应该收尾，
    则通过 on_stream / on_stream_end 推送最终内容（如 Feishu 卡片更新），
    避免流式通道卡片留空。

    Args:
        stop_reason: runner 终止原因（"max_iterations" / "completed" / "error" 等）。
        pending_queue_available: 本 turn 是否持有 pending queue（可排班续跑）。
        session_metadata: 会话 metadata。
        message_metadata: 当前消息 metadata（可空）。

    Returns:
        True 表示应通过 stream 推送最终响应；False 表示不推送。
    """
    if stop_reason != "max_iterations":
        return False
    # 可续跑时不推送（由隐形续跑接管，最终响应由续跑片产出）。
    return should_finalize_on_max_iterations(
        pending_queue_available=pending_queue_available,
        session_metadata=session_metadata,
        message_metadata=message_metadata,
    )


async def maybe_continue_turn(ctx: Any) -> bool:
    """当策略允许时，为 *ctx* 排班一条隐形续跑消息。

    成功时：
    - 向 ``ctx.pending_queue`` 放入续跑 InboundMessage；
    - 把 ``ctx.msg.metadata`` 打上 pending 标记（供 dispatch 抑制事件、
      最终响应交给续跑片）；
    - 清空 ``ctx.final_content``、剥离最后一条合成的 assistant 消息
      （max_iterations fallback 文本不落历史）、``ctx.suppress_response=True``；
    - 会话 metadata 轮次计数 +1（上限 12）。

    Args:
        ctx: 本 turn 的上下文载体（鸭子类型：需 session / pending_queue /
            stop_reason / final_content / all_messages / msg / session_key /
            visible_run_started_at 等字段）。

    Returns:
        True 表示已排班续跑；False 表示策略不允许（预算未到、无 queue、
        无活跃 goal、轮次达上限等）。
    """
    if ctx.session is None or ctx.pending_queue is None:
        return False
    if not _continuation_available(
        stop_reason=ctx.stop_reason,
        pending_queue_available=True,
        session_metadata=ctx.session.metadata,
        message_metadata=getattr(ctx.msg, "metadata", None),
    ):
        return False

    metadata = _internal_continuation_metadata(
        getattr(ctx.msg, "metadata", None),
        run_started_at=getattr(ctx, "visible_run_started_at", None),
    )
    content = _goal_continuation_prompt(ctx.session.metadata)
    messages = _strip_terminal_assistant(getattr(ctx, "all_messages", []), ctx.final_content)
    _increment_goal_continuation_round(ctx.session.metadata)

    ctx.msg.metadata[INTERNAL_CONTINUATION_PENDING_META] = True
    ctx.final_content = ""
    ctx.all_messages = messages
    ctx.suppress_response = True
    await ctx.pending_queue.put(
        dataclasses.replace(
            ctx.msg,
            sender_id=_GOAL_CONTINUATION_SENDER,
            content=content,
            media=[],
            metadata=metadata,
            session_key_override=ctx.session_key,
        )
    )
    return True


def prepare_save_boundary(ctx: Any) -> None:
    """准备续跑簿记与本轮历史 append 边界。

    - 会话级轮次计数在 goal 不再活跃时复位（新 goal 重新计费）；
    - 计算 ``ctx.save_skip``：持久化 append 的起始下标（替换旧硬编码）。

    Args:
        ctx: turn 上下文（需 session / save_skip 属性；续跑语义需
            msg.metadata / initial_messages / history / user_persisted_early）。
    """
    if ctx.session is not None:
        clear_internal_continuation_state(ctx.session.metadata)

    ctx.save_skip = _save_skip_for_turn(
        message_metadata=getattr(ctx.msg, "metadata", None),
        initial_message_count=len(getattr(ctx, "initial_messages", [])),
        history_count=len(getattr(ctx, "history", [])),
        user_persisted_early=getattr(ctx, "user_persisted_early", False),
    )


def _continuation_available(
    *,
    stop_reason: str,
    pending_queue_available: bool,
    session_metadata: Mapping[str, Any] | None,
    message_metadata: Mapping[str, Any] | None = None,
) -> bool:
    """策略汇总：预算边界 + queue 可用 + goal 续跑可用。"""
    if stop_reason != "max_iterations" or not pending_queue_available:
        return False
    return _goal_continuation_available(
        session_metadata,
        message_metadata=message_metadata,
    )


def clear_internal_continuation_state(metadata: MutableMapping[str, Any]) -> None:
    """运行时模式退出时复位续跑簿记。

    Args:
        metadata: 会话 metadata（原地修改）。
    """
    if not sustained_goal_active(metadata):
        reset_goal_continuation_rounds(metadata)


def reset_goal_continuation_rounds(metadata: MutableMapping[str, Any]) -> None:
    """新 goal 从全新续跑预算开始。

    Args:
        metadata: 会话 metadata（原地修改）。
    """
    metadata.pop(_GOAL_CONTINUATION_ROUNDS_KEY, None)


def _save_skip_for_turn(
    *,
    message_metadata: Mapping[str, Any] | None,
    initial_message_count: int,
    history_count: int,
    user_persisted_early: bool,
) -> int:
    """计算本轮持久化 append 边界（对齐 nanobot 同名私有函数）。

    三种形态：
    - 续跑 / ``_skip_user_persist``：user 未持久化，从 initial 起点 append；
    - 当前用户消息被并入历史同角色尾部（build_messages 合并）：边界 =
      initial_message_count（历史 + 合并行已含当前输入）；
    - 独立未持久化（user_persisted_early=False 的 standalone）：往前挪一位
      跳过尚未持久化的当前 user 行。

    Args:
        message_metadata: 当前消息 metadata。
        initial_message_count: 本轮 initial_messages 行数。
        history_count: 本轮 get_history 行数。
        user_persisted_early: 当前用户消息是否已提前持久化。

    Returns:
        持久化 append 起点下标。
    """
    if message_metadata and message_metadata.get(SKIP_USER_PERSIST_META) is True:
        return initial_message_count
    if internal_continuation_inbound(message_metadata):
        return initial_message_count
    has_standalone_current = initial_message_count > 1 + history_count
    if has_standalone_current and not user_persisted_early and history_count == 0:
        return initial_message_count - 1
    return initial_message_count


def _goal_continuation_available(
    session_metadata: Mapping[str, Any] | None,
    *,
    message_metadata: Mapping[str, Any] | None = None,
    max_rounds: int = _MAX_GOAL_CONTINUATION_ROUNDS,
) -> bool:
    """goal 续跑可用性：活跃/显式请求且轮次未超上限。"""
    if not sustained_goal_turn(session_metadata, message_metadata=message_metadata):
        return False
    if not sustained_goal_active(session_metadata):
        return False
    try:
        rounds = int((session_metadata or {}).get(_GOAL_CONTINUATION_ROUNDS_KEY) or 0)
    except (TypeError, ValueError):
        rounds = 0
    return rounds < max(0, max_rounds)


def _increment_goal_continuation_round(metadata: MutableMapping[str, Any]) -> None:
    """会话级续跑轮次 +1。"""
    try:
        rounds = int(metadata.get(_GOAL_CONTINUATION_ROUNDS_KEY) or 0)
    except (TypeError, ValueError):
        rounds = 0
    metadata[_GOAL_CONTINUATION_ROUNDS_KEY] = rounds + 1


def _internal_continuation_metadata(
    message_metadata: Mapping[str, Any] | None,
    *,
    run_started_at: float | None = None,
) -> dict[str, Any]:
    """为续跑消息构造 metadata：打续跑标记、传播运行起点、剥 terminal 键。"""
    metadata = dict(message_metadata or {})
    metadata[INTERNAL_CONTINUATION_META] = True
    metadata[INTERNAL_CONTINUATION_KIND_META] = _GOAL_CONTINUATION_KIND
    if run_started_at is not None:
        metadata[INTERNAL_CONTINUATION_RUN_STARTED_AT_META] = float(run_started_at)
    for key in _STRIPPED_INBOUND_META_KEYS:
        metadata.pop(key, None)
    return metadata


def _goal_continuation_prompt(metadata: Mapping[str, Any] | None) -> str:
    """组装续跑片的提示文本（含活跃 goal 的运行状态行）。"""
    lines = goal_state_runtime_lines(metadata)
    if lines:
        goal = "\n".join(lines)
        return (
            "Continue the active sustained goal after the previous turn reached "
            "its tool-call budget.\n\n"
            f"{goal}\n\n"
            "Continue from the saved context. Do not mention the continuation "
            "boundary to the user. Use tools as needed, and call update_goal "
            "with action='complete' when the objective is truly finished."
        )
    return (
        "Continue the active sustained goal after the previous turn reached "
        "its tool-call budget. Continue from the saved context. Do not mention "
        "the continuation boundary to the user. Use tools as needed, and call "
        "update_goal with action='complete' when the objective is truly finished."
    )


def _strip_terminal_assistant(
    messages: list[dict[str, Any]],
    final_content: str | None,
) -> list[dict[str, Any]]:
    """保存历史前剥掉 max_iterations 合成的最后一条 assistant 消息。

    仅当尾条消息的 content 恰等于 final_content 且无 tool_calls 时才剥
    （跨模型真实回复不会被误删——max_iterations fallback 文本是 runner
    在迭代耗尽时合成的，内容与 final_content 精确一致）。
    """
    if not messages:
        return messages
    last = messages[-1]
    if last.get("role") != "assistant":
        return messages
    if final_content is None or last.get("content") != final_content:
        return messages
    if last.get("tool_calls"):
        return messages
    return messages[:-1]