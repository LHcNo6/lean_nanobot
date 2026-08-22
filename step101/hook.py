from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from step101.helpers import IncrementalThinkExtractor, strip_think
from step101.utils.progress_events import (
    build_tool_event_finish_payloads,
    build_tool_event_start_payload,
    invoke_on_progress,
    on_progress_accepts_tool_events,
)
from step101.utils.tool_hints import format_tool_hints

logger = logging.getLogger(__name__)


@dataclass
class AgentHookContext:
    """一次迭代（iteration）内可变的状态，暴露给 runner hooks。

    Attributes:
        iteration: 迭代序号（从 0 起）。
        messages: 当前会话消息列表（runner 原地修改）。
        session_key: 会话键。
        response: 本次迭代的 LLM 响应。
        usage: 累计 usage。
        tool_calls: 本次迭代的工具调用列表。
        tool_results: 与 tool_calls 对齐的工具结果列表。
        tool_events: 与 tool_calls 对齐的 ``{name, status, detail}`` 事件。
        streamed_content: 内容是否已通过 on_stream 流式发出。
        streamed_reasoning: 推理是否已流式/显式发出。
        final_content: 最终内容。
        stop_reason: 终止原因。
        error: 错误文本。
    """

    iteration: int
    messages: list[dict[str, Any]]
    session_key: str | None = None
    response: Any | None = None
    usage: dict[str, int] = field(default_factory=dict)
    tool_calls: list[Any] = field(default_factory=list)
    tool_results: list[Any] = field(default_factory=list)
    tool_events: list[dict[str, str]] = field(default_factory=list)
    streamed_content: bool = False
    streamed_reasoning: bool = False
    final_content: str | None = None
    stop_reason: str | None = None
    error: str | None = None
    stream_content: str = ""


@dataclass
class AgentRunHookContext:
    """一次 run 结束时的状态快照（对齐 nanobot ``AgentRunHookContext``）。"""

    messages: list[dict[str, Any]]
    final_content: str | None = None
    tools_used: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    stop_reason: str | None = None
    error: str | None = None
    tool_events: list[dict[str, str]] = field(default_factory=list)
    had_injections: bool = False
    exception: BaseException | None = None


@dataclass
class AgentTurnHookContext:
    """构造 per-turn hook 时可用的 turn 级输入（对齐 nanobot 同名类）。

    Attributes:
        on_progress: 进度回调（content 必填，可选 tool_hint/tool_events/
            reasoning/reasoning_end 关键字）。
        workspace: 本 turn 的 workspace 路径。
        channel / chat_id / message_id / session_key: 消息路由信息。
        metadata: 消息元数据。
        ephemeral: 是否临时 turn。
    """

    on_progress: Callable[..., Awaitable[None]] | None = None
    workspace: str | None = None
    channel: str = "cli"
    chat_id: str = "direct"
    message_id: str | None = None
    session_key: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    ephemeral: bool = False


class AgentHook:
    """共享 runner 定制的最小生命周期接口。

    所有方法默认空实现，子类按需覆写。``on_stream_end`` 带 ``resuming``
    语义：True 表示流保持存活（续跑/注入接管），False 表示正常结束。
    """

    def __init__(self, reraise: bool = False) -> None:
        self._reraise = reraise

    def wants_streaming(self) -> bool:
        """是否期望以 delta 流式交付内容（决定 runner 走流式调用）。"""
        return False

    async def before_run(self, context: AgentRunHookContext) -> None:
        ...

    async def after_run(self, context: AgentRunHookContext) -> None:
        ...

    async def on_error(self, context: AgentRunHookContext) -> None:
        ...

    async def on_finally(self, context: AgentRunHookContext) -> None:
        ...

    async def before_iteration(self, context: AgentHookContext) -> None:
        ...

    async def after_iteration(self, context: AgentHookContext) -> None:
        ...

    async def on_stream(self, context: AgentHookContext, delta: str) -> None:
        ...

    async def on_stream_end(
        self, context: AgentHookContext, *, resuming: bool = False
    ) -> None:
        ...

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        ...

    async def before_execute_tool(
        self,
        context: AgentHookContext,
        tool_call: Any,
        tool: Any,
        params: Any,
    ) -> None:
        ...

    async def after_execute_tool(
        self,
        context: AgentHookContext,
        tool_call: Any,
        tool: Any,
        params: Any,
        result: Any,
    ) -> None:
        ...

    async def on_execute_tool_error(
        self,
        context: AgentHookContext,
        tool_call: Any,
        tool: Any,
        params: Any,
        error: Any,
    ) -> None:
        ...

    async def emit_reasoning(self, reasoning_content: str | None) -> None:
        """发布一条推理内容（一次或增量片段）。"""
        ...

    async def emit_reasoning_end(self) -> None:
        """标记进行中的推理流结束（缓冲类 hook 在此冲刷冻结）。"""
        ...

    def finalize_content(
        self, context: AgentHookContext, content: str | None
    ) -> str | None:
        """内容净化管线：返回用于持久化/展示的最终内容（同步）。"""
        return content


AgentTurnHookFactory = Callable[[AgentTurnHookContext], AgentHook | None]


class CompositeHook(AgentHook):
    """把多个 hook 按顺序 fan-out 的组合 hook。

    错误隔离：async 方法逐个捕获并记日志，单个坏 hook 不能崩溃 agent 循环
    （``reraise=True`` 的 hook 例外，其异常向上传播）。``finalize_content``
    是同步管线（不隔离——内容 bug 应该暴露）。
    """

    def __init__(self, hooks: list[AgentHook]) -> None:
        super().__init__()
        self._hooks = list(hooks)

    def wants_streaming(self) -> bool:
        return any(h.wants_streaming() for h in self._hooks)

    async def _for_each_hook_safe(
        self, method_name: str, *args: Any, **kwargs: Any
    ) -> None:
        for hook in self._hooks:
            if getattr(hook, "_reraise", False):
                await getattr(hook, method_name)(*args, **kwargs)
                continue
            try:
                await getattr(hook, method_name)(*args, **kwargs)
            except Exception as exc:
                logger.exception(
                    "Hook %s.%s failed: %s", type(hook).__name__, method_name, exc
                )

    async def before_run(self, context: AgentRunHookContext) -> None:
        await self._for_each_hook_safe("before_run", context)

    async def after_run(self, context: AgentRunHookContext) -> None:
        await self._for_each_hook_safe("after_run", context)

    async def on_error(self, context: AgentRunHookContext) -> None:
        await self._for_each_hook_safe("on_error", context)

    async def on_finally(self, context: AgentRunHookContext) -> None:
        await self._for_each_hook_safe("on_finally", context)

    async def before_iteration(self, context: AgentHookContext) -> None:
        await self._for_each_hook_safe("before_iteration", context)

    async def after_iteration(self, context: AgentHookContext) -> None:
        await self._for_each_hook_safe("after_iteration", context)

    async def on_stream(self, context: AgentHookContext, delta: str) -> None:
        await self._for_each_hook_safe("on_stream", context, delta)

    async def on_stream_end(
        self, context: AgentHookContext, *, resuming: bool = False
    ) -> None:
        await self._for_each_hook_safe("on_stream_end", context, resuming=resuming)

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        await self._for_each_hook_safe("before_execute_tools", context)

    async def before_execute_tool(
        self,
        context: AgentHookContext,
        tool_call: Any,
        tool: Any,
        params: Any,
    ) -> None:
        await self._for_each_hook_safe(
            "before_execute_tool", context, tool_call, tool, params
        )

    async def after_execute_tool(
        self,
        context: AgentHookContext,
        tool_call: Any,
        tool: Any,
        params: Any,
        result: Any,
    ) -> None:
        await self._for_each_hook_safe(
            "after_execute_tool", context, tool_call, tool, params, result
        )

    async def on_execute_tool_error(
        self,
        context: AgentHookContext,
        tool_call: Any,
        tool: Any,
        params: Any,
        error: Any,
    ) -> None:
        await self._for_each_hook_safe(
            "on_execute_tool_error", context, tool_call, tool, params, error
        )

    async def emit_reasoning(self, reasoning_content: str | None) -> None:
        await self._for_each_hook_safe("emit_reasoning", reasoning_content)

    async def emit_reasoning_end(self) -> None:
        await self._for_each_hook_safe("emit_reasoning_end")

    def finalize_content(
        self, context: AgentHookContext, content: str | None
    ) -> str | None:
        for hook in self._hooks:
            content = hook.finalize_content(context, content)
        return content


@dataclass
class AgentTurnHookSpec:
    """一次 agent turn 构建 hook 链所需的全部输入（对齐 nanobot 同名类）。

    Attributes:
        on_progress: 进度回调（bus 回调，接受 tool_hint/tool_events/
            reasoning/reasoning_end）。
        on_stream: 内容 delta 回调。
        on_stream_end: 流结束回调。
        channel / chat_id / message_id: 消息路由信息。
        metadata: 消息元数据。
        session_key: 会话键。
        workspace: workspace 路径。
        tool_hint_max_length: 工具提示最大长度。
        on_iteration: 每次迭代开始的同步回调（可观测 iteration）。
        registered_hook_factories: run 级注册的 hook 工厂（先执行）。
        turn_hook_factories: turn 级 hook 工厂（后执行）。
        registered_hooks: run 级静态 hook 列表。
        turn_hooks: turn 级静态 hook 列表。
        ephemeral: 是否临时 turn（临时 turn 只保留 progress hook）。
        run_extra_hooks_for_ephemeral: 临时 turn 是否也跑额外 hook。
    """

    on_progress: Callable[..., Awaitable[None]] | None = None
    on_stream: Callable[[str], Awaitable[None]] | None = None
    on_stream_end: Callable[..., Awaitable[None]] | None = None
    channel: str = "cli"
    chat_id: str = "direct"
    message_id: str | None = None
    metadata: dict[str, Any] | None = None
    session_key: str | None = None
    workspace: str | None = None
    tool_hint_max_length: int = 40
    on_iteration: Callable[[int], None] | None = None
    registered_hook_factories: list[AgentTurnHookFactory] = field(default_factory=list)
    turn_hook_factories: list[AgentTurnHookFactory] = field(default_factory=list)
    registered_hooks: list[AgentHook] = field(default_factory=list)
    turn_hooks: list[AgentHook] = field(default_factory=list)
    ephemeral: bool = False
    run_extra_hooks_for_ephemeral: bool = False


def build_agent_turn_hook(spec: AgentTurnHookSpec) -> AgentHook:
    """装配一次 turn 的 hook 链（对齐 nanobot ``turn_hooks.build_agent_turn_hook``）。

    hook 顺序：progress hook（底座）→ run 级注册工厂 → run 级静态 hook →
    turn 级工厂 → turn 级静态 hook。工厂抛异常时记日志并跳过（单个坏工厂
    不阻塞装配）。链只有 progress hook 时直接返回它，避免无谓包装。
    """
    progress_hook = AgentProgressHook(
        on_progress=spec.on_progress,
        on_stream=spec.on_stream,
        on_stream_end=spec.on_stream_end,
        session_key=spec.session_key,
        tool_hint_max_length=spec.tool_hint_max_length,
        on_iteration=spec.on_iteration,
    )
    if spec.ephemeral and not spec.run_extra_hooks_for_ephemeral:
        return progress_hook

    turn_context = AgentTurnHookContext(
        on_progress=spec.on_progress,
        workspace=spec.workspace,
        channel=spec.channel,
        chat_id=spec.chat_id,
        message_id=spec.message_id,
        session_key=spec.session_key,
        metadata=dict(spec.metadata or {}),
        ephemeral=spec.ephemeral,
    )
    hook_chain: list[AgentHook] = [progress_hook]

    for factory in [*spec.registered_hook_factories, *spec.turn_hook_factories]:
        try:
            created_hook = factory(turn_context)
        except Exception:
            logger.exception("Agent turn hook factory failed: %s", factory)
            continue
        if created_hook is not None:
            hook_chain.append(created_hook)

    hook_chain.extend(spec.registered_hooks)
    hook_chain.extend(spec.turn_hooks)
    return CompositeHook(hook_chain) if len(hook_chain) > 1 else progress_hook


class AgentProgressHook(AgentHook):
    """把 runner 生命周期事件翻译成用户可见的进度信号（对齐 nanobot
    ``progress_hook.py``）。

    职责：
    - 流式内容里增量提取 ``<think>`` 推理（``IncrementalThinkExtractor``）；
    - 推理片段经 ``emit_reasoning`` 发布（on_progress(reasoning=True)）；
    - 工具执行前后发布 tool_hint 与 tool_events（start/finish payload）；
    - ``finalize_content`` 剥掉残留 think 标签，净化最终内容。
    """

    def __init__(
        self,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        *,
        session_key: str | None = None,
        tool_hint_max_length: int = 40,
        on_iteration: Callable[[int], None] | None = None,
    ) -> None:
        super().__init__(reraise=True)
        self._on_progress = on_progress
        self._on_stream = on_stream
        self._on_stream_end = on_stream_end
        self._session_key = session_key
        self._tool_hint_max_length = tool_hint_max_length
        self._on_iteration = on_iteration
        self._stream_buf = ""
        self._think_extractor = IncrementalThinkExtractor()
        self._reasoning_open = False

    def wants_streaming(self) -> bool:
        return self._on_stream is not None

    @staticmethod
    def _strip_think(text: str | None) -> str | None:
        if not text:
            return None
        return strip_think(text) or None

    def _tool_hint(self, tool_calls: list[Any]) -> str:
        return format_tool_hints(tool_calls, max_length=self._tool_hint_max_length)

    @staticmethod
    def _on_progress_accepts(cb: Callable[..., Any], name: str) -> bool:
        """探测 on_progress 是否接受关键字参数 *name*。"""
        try:
            sig = inspect.signature(cb)
        except (TypeError, ValueError):
            return False
        if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
            return True
        return name in sig.parameters

    async def on_stream(self, context: AgentHookContext, delta: str) -> None:
        """消费内容 delta：先提取增量推理，剩余净化文本交给 on_stream。"""
        prev_clean = strip_think(self._stream_buf)
        self._stream_buf += delta
        new_clean = strip_think(self._stream_buf)
        incremental = new_clean[len(prev_clean):]

        if await self._think_extractor.feed(self._stream_buf, self.emit_reasoning):
            context.streamed_reasoning = True

        if incremental:
            # 答案文本开始；先关闭推理段，让 UI 在答案渲染前锁定气泡。
            await self.emit_reasoning_end()
            if self._on_stream:
                await self._on_stream(incremental)

    async def on_stream_end(
        self, context: AgentHookContext, *, resuming: bool = False
    ) -> None:
        await self.emit_reasoning_end()
        if self._on_stream_end:
            await self._on_stream_end(resuming=resuming)
        self._stream_buf = ""
        self._think_extractor.reset()

    async def before_iteration(self, context: AgentHookContext) -> None:
        if self._on_iteration:
            self._on_iteration(context.iteration)
        logger.debug(
            "Starting agent loop iteration %d for session %s",
            context.iteration, self._session_key,
        )

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        """工具执行前：发布思考文本 + tool_hint + tool_events(start)。"""
        if self._on_progress:
            if not self._on_stream and not context.streamed_content:
                thought = self._strip_think(
                    context.response.content if context.response else None
                )
                if thought:
                    await self._on_progress(thought)
            tool_hint = self._strip_think(self._tool_hint(context.tool_calls))
            tool_events = [build_tool_event_start_payload(tc) for tc in context.tool_calls]
            await invoke_on_progress(
                self._on_progress,
                tool_hint or "",
                tool_hint=True,
                tool_events=tool_events,
            )

    async def emit_reasoning(self, reasoning_content: str | None) -> None:
        """发布一条推理片段；通道插件决定是否渲染。"""
        if (
            self._on_progress
            and reasoning_content
            and self._on_progress_accepts(self._on_progress, "reasoning")
        ):
            self._reasoning_open = True
            await self._on_progress(reasoning_content, reasoning=True)

    async def emit_reasoning_end(self) -> None:
        """关闭当前推理流段（若有打开）。"""
        if self._reasoning_open and self._on_progress:
            self._reasoning_open = False
            await self._on_progress("", reasoning_end=True)
        else:
            self._reasoning_open = False

    async def after_iteration(self, context: AgentHookContext) -> None:
        """迭代结束：发布 tool_events(finish) + 记录 usage。"""
        if (
            self._on_progress
            and context.tool_calls
            and context.tool_events
            and on_progress_accepts_tool_events(self._on_progress)
        ):
            tool_events = build_tool_event_finish_payloads(context)
            if tool_events:
                await invoke_on_progress(
                    self._on_progress,
                    "",
                    tool_hint=False,
                    tool_events=tool_events,
                )
        u = context.usage or {}
        logger.debug(
            "LLM usage: prompt=%s completion=%s",
            u.get("prompt_tokens", 0), u.get("completion_tokens", 0),
        )

    def finalize_content(
        self, context: AgentHookContext, content: str | None
    ) -> str | None:
        return self._strip_think(content)
