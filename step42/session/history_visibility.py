"""持久化历史的可见性辅助（对齐 nanobot ``session/history_visibility.py``）。

A12：把"仅供模型/内部流程消费、不应作为聊天 turn 展示"的消息打上
``_hidden_history`` 标记。标记值可以是 ``True`` 或 dict（如
``{"kind": "subagent_result"}``），dict 形式便于携带额外标识。

使用点：
- runner 注入消息合并防护（带隐藏标记的 user 行不并入上一行 user，保证
  标记与角色交替语义不丢）；
- ``/history`` 等展示路径过滤；
- 注意：**get_history 不过滤隐藏行**（对齐 nanobot——它们要保留在 LLM
  上下文中），只有 ``_command`` 行被跳过（lean 命令对不持久化，暂不涉及）。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

HIDDEN_HISTORY_META = "_hidden_history"


def _has_hidden_history_marker(message: Mapping[str, Any] | None) -> bool:
    """判定单条消息是否携带隐藏历史标记。"""
    if not message:
        return False
    marker = message.get(HIDDEN_HISTORY_META)
    return marker is True or isinstance(marker, Mapping)


def is_hidden_history_message(message: Mapping[str, Any] | None) -> bool:
    """判断一条持久化消息是否应藏而不展示为聊天轮次。

    Args:
        message: 会话历史中的一条消息（dict 或近似映射）。

    Returns:
        True 表示带隐藏标记（应隐藏）。
    """
    return _has_hidden_history_marker(message)