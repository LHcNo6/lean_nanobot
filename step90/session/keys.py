"""会话键常量与统一会话推导（对齐 nanobot ``session/keys.py``）。

H8：nanobot 以 ``unified:default`` 作为统一会话键、``channel:chat_id``
作为通道会话键（都直接作为 session 主键使用，与既有 ``base64url`` 存储
编码衔接——lean 的 ``SessionManager._storage_key`` 对任意键做编码，因此
键格式变化不会破坏持久化布局）。
"""

from __future__ import annotations

UNIFIED_SESSION_KEY = "unified:default"


def session_key_for_channel(
    channel: str,
    chat_id: str,
    *,
    unified_session: bool = False,
) -> str:
    """返回某个通道/会话对使用的会话键。

    Args:
        channel: 通道名（如 ``"cli"`` / ``"system"``）。
        chat_id: 通道内聊天标识。
        unified_session: True 时统一为 ``UNIFIED_SESSION_KEY``
            （单用户多设备共享一个会话，对齐 nanobot ``agents.defaults.unified_session``）。

    Returns:
        会话键字符串。
    """
    if unified_session:
        return UNIFIED_SESSION_KEY
    return f"{channel}:{chat_id}"