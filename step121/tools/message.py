"""极简消息发送工具（step42 最小增量版）。

对齐 nanobot ``MessageTool`` 的核心机制：
- ``_sent_in_turn`` 标记：本 turn 是否已通过本工具直接发送消息
- ``start_turn()``：每个 turn 开始时重置标记
- ``execute()``：发送消息后置 ``_sent_in_turn = True``

最小增量取舍：
- 用简单 bool 而非 ContextVar（step42 单 turn 顺序执行，无并发）
- 不实现 media/buttons/跨通道/workspace 安全检测
- ``send_callback`` 由 ``create()`` 从 ``ctx.bus`` 获取
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from step121.schema import StringSchema, tool_parameters_schema
from step121.tool import Tool, ToolResult, tool_parameters


@tool_parameters(tool_parameters_schema(
    content=StringSchema("Message content to send proactively."),
    required=["content"],
))
class MessageTool(Tool):
    """主动消息发送工具（step42 极简版）。

    LLM 调用本工具直接发送消息后，``_assemble_outbound`` 会检测
    ``_sent_in_turn`` 并抑制重复出站。
    """

    def __init__(
        self,
        send_callback: Callable[..., Awaitable[None]] | None = None,
    ) -> None:
        self._send_callback = send_callback
        # step42 最小增量：简单 bool，不用 ContextVar
        self._sent_in_turn: bool = False

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        """ToolLoader 自动调用，从 ToolContext 获取 bus 作为 send_callback。"""
        send_callback = ctx.bus.publish_outbound if ctx.bus else None
        return cls(send_callback=send_callback)

    def start_turn(self) -> None:
        """每个 turn 开始时重置发送标记（由 ``_state_build`` 调用）。"""
        self._sent_in_turn = False

    @property
    def name(self) -> str:
        return "message"

    @property
    def description(self) -> str:
        return (
            "Proactively send a message to the user. "
            "Do not use this for a normal reply — answer naturally instead."
        )

    async def execute(self, content: str = "", **kwargs: Any) -> ToolResult:
        """发送消息并标记 ``_sent_in_turn = True``。

        Args:
            content: 消息内容。

        Returns:
            成功返回确认文案，未配置 send_callback 返回错误。
        """
        from step121.bus.events import OutboundMessage

        if not self._send_callback:
            return ToolResult.error("Error: Message sending not configured")

        await self._send_callback(OutboundMessage(content=content))
        # step42 极简版：只要发送就标记（不区分是否当前通道）
        self._sent_in_turn = True
        return "Message sent"
