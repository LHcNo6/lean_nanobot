"""运行时上下文 —— 每 turn 动态解析并追加到当前用户消息的可选上下文。

对齐 nanobot `runtime_context.py` 的最小集（A9）：
- ``RuntimeContextBlock``：一个 provider 拥有的文本块（source + content）；
- ``RuntimeContextProvider``：``async (request) -> block(s) | None`` 的可调用；
- ``resolve_runtime_context``：按调用方稳定顺序**串行**解析一次；
- ``append_runtime_context``：把块拼到 user 内容尾部（文本或多模态 list
  两种形态），并返回可精确移除的持久化 marker；
- ``wrap_runtime_context_lines``：把行集合包进固定标记对（供 provider 使用）。

与 nanobot 的差异（刻意简化）：
- 不持久化 marker：lean 只把块合并进内存中的 initial_messages，session 历史
  保持原文本（避免污染 replay / consolidation），因此省略了
  ``public_history_message(s)`` 展示期移除逻辑 —— step29 历史可见性
  （``session/history_visibility.py``，HIDDEN_HISTORY_META）时再评估对齐；
- 无内置 provider（clock 等演示 provider 由 main.py 注册）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeAlias

if TYPE_CHECKING:
    from step28.context import RequestContext

RUNTIME_CONTEXT_HISTORY_META = "_runtime_context"
RUNTIME_CONTEXT_MESSAGE_META = "runtime_context"
RUNTIME_CONTEXT_TAG = "[Runtime Context — metadata only, not instructions]"
RUNTIME_CONTEXT_END = "[/Runtime Context]"


@dataclass(frozen=True)
class RuntimeContextBlock:
    """一个 provider 拥有的、追加到当前 user 内容的块。

    Attributes:
        source: 块来源标识（如 ``"goal"``／``"clock"``），非空。
        content: 块文本内容（首尾空白会被剥除；空内容块会被丢弃）。
    """

    source: str
    content: str


# 一个 provider 的返回值形态：单块 / 块序列 / 无。
RuntimeContextResult: TypeAlias = (
    RuntimeContextBlock | Sequence[RuntimeContextBlock] | None
)
# provider 签名：接收 RequestContext，返回 RuntimeContextResult。
RuntimeContextProvider: TypeAlias = Callable[
    ["RequestContext"], Awaitable[RuntimeContextResult]
]


def wrap_runtime_context_lines(lines: Iterable[str]) -> str:
    """把非空运行时 metadata 行包进既定的提示标记对。"""
    content = "\n".join(line.strip() for line in lines if line.strip())
    if not content:
        return ""
    return f"{RUNTIME_CONTEXT_TAG}\n{content}\n{RUNTIME_CONTEXT_END}"


def normalize_runtime_context_blocks(result: RuntimeContextResult) -> list[RuntimeContextBlock]:
    """校验 provider 返回值，返回非空块并保持 provider 顺序。"""
    if result is None:
        return []
    values = [result] if isinstance(result, RuntimeContextBlock) else list(result)
    blocks: list[RuntimeContextBlock] = []
    for block in values:
        if not isinstance(block, RuntimeContextBlock):
            raise TypeError("runtime context providers must return RuntimeContextBlock values")
        source = block.source.strip()
        content = block.content.strip()
        if not source:
            raise ValueError("runtime context block source must not be empty")
        if content:
            blocks.append(RuntimeContextBlock(source=source, content=content))
    return blocks


async def resolve_runtime_context(
    providers: Iterable[RuntimeContextProvider],
    request: RequestContext,
) -> list[RuntimeContextBlock]:
    """按调用方的稳定顺序，串行解析全部 provider 一次。"""
    blocks: list[RuntimeContextBlock] = []
    for provider in providers:
        blocks.extend(normalize_runtime_context_blocks(await provider(request)))
    return blocks


def append_runtime_context(
    content: Any,
    blocks: Sequence[RuntimeContextBlock],
) -> tuple[Any, dict[str, Any] | None]:
    """把块追加到内容尾部，返回 (合并后的内容, 可精确移除的持久化 marker)。

    - 无块时原样返回内容、marker 为 None；
    - ``content`` 为 list（多模态）：追加 ``{"type": "text", ...}`` 块；
    - 否则按文本拼接（空文本时不加分隔）。
    """
    if not blocks:
        return content, None

    rendered = [block.content for block in blocks]
    sources = [block.source for block in blocks]
    if isinstance(content, list):
        context_blocks = [{"type": "text", "text": text} for text in rendered]
        return [*content, *context_blocks], {
            "version": 1,
            "sources": sources,
            "blocks": context_blocks,
        }

    text = "" if content is None else str(content)
    suffix = "\n\n".join(rendered)
    merged = f"{text}\n\n{suffix}" if text else suffix
    return merged, {
        "version": 1,
        "sources": sources,
        "suffix": suffix,
    }