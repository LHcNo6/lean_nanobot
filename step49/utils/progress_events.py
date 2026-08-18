"""结构化进度事件辅助函数（对齐 nanobot ``utils/progress_events.py``）。

把 runner 的工具执行生命周期翻译成 UI 友好的 ``tool_events`` payload：
- ``build_tool_event_start_payload``：工具开始（phase=start，带参数快照）；
- ``build_tool_event_finish_payloads``：迭代结束时按 context 对齐输出
  end/error 两个终态；
- ``invoke_on_progress`` / ``on_progress_accepts_tool_events``：回调签名
  探测——只把 tool_events 传给接受该参数的 on_progress。
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any


def on_progress_accepts_tool_events(cb: Callable[..., Any]) -> bool:
    """探测 on_progress 回调是否接受 ``tool_events`` 关键字参数。"""
    try:
        sig = inspect.signature(cb)
    except (TypeError, ValueError):
        return False
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
        return True
    return "tool_events" in sig.parameters


async def invoke_on_progress(
    on_progress: Callable[..., Awaitable[None]],
    content: str,
    *,
    tool_hint: bool = False,
    tool_events: list[dict[str, Any]] | None = None,
) -> None:
    """调用 on_progress，仅在回调接受时附加 tool_events。"""
    if tool_events and on_progress_accepts_tool_events(on_progress):
        await on_progress(content, tool_hint=tool_hint, tool_events=tool_events)
        return
    await on_progress(content, tool_hint=tool_hint)


def _tool_event_arguments(tool_call: Any) -> dict[str, Any]:
    arguments = getattr(tool_call, "arguments", {}) or {}
    return arguments if isinstance(arguments, dict) else {}


def build_tool_event_start_payload(tool_call: Any) -> dict[str, Any]:
    """构建工具开始事件 payload（对齐 nanobot 同名字段）。"""
    return {
        "version": 1,
        "phase": "start",
        "call_id": str(getattr(tool_call, "id", "") or ""),
        "name": getattr(tool_call, "name", ""),
        "arguments": _tool_event_arguments(tool_call),
        "result": None,
        "error": None,
        "files": [],
        "embeds": [],
    }


def build_tool_event_finish_payloads(context: Any) -> list[dict[str, Any]]:
    """按 context 的 tool_calls/tool_results/tool_events 对齐输出终态 payload。

    Args:
        context: AgentHookContext（含 tool_calls / tool_results / tool_events）。

    Returns:
        phase=end（成功）或 phase=error（失败）的 payload 列表。
    """
    payloads: list[dict[str, Any]] = []
    count = min(len(context.tool_calls), len(context.tool_results), len(context.tool_events))
    for idx in range(count):
        tool_call = context.tool_calls[idx]
        result = context.tool_results[idx]
        event = context.tool_events[idx] if isinstance(context.tool_events[idx], dict) else {}
        status = event.get("status")
        phase = "end" if status == "ok" else "error"
        payload = {
            "version": 1,
            "phase": phase,
            "call_id": str(getattr(tool_call, "id", "") or ""),
            "name": getattr(tool_call, "name", ""),
            "arguments": _tool_event_arguments(tool_call),
            "result": result if phase == "end" else None,
            "error": None,
            "files": [],
            "embeds": [],
        }
        if phase == "error":
            if isinstance(result, str) and result.strip():
                payload["error"] = result.strip()
            else:
                payload["error"] = str(event.get("detail") or "Tool execution failed")
        payloads.append(payload)
    return payloads
