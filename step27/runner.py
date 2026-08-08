from __future__ import annotations

import asyncio
import inspect
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from step27.governance import ContextGovernanceConfig, ContextGovernor
from step27.hook import AgentHook, AgentHookContext, AgentRunHookContext
from step27.llm import LLMResponse
from step27.provider import LLMProvider
from step27.tool import Tool, ToolRegistry


logger = logging.getLogger(__name__)


_GOVERNOR = ContextGovernor()

_MAX_CONCURRENT_TOOLS = 10
_MAX_MALFORMED_TOOL_RETRIES = 1
_MALFORMED_TOOL_RETRY_MESSAGE = (
    "The previous response contained tool calls with invalid names. "
    "Please retry using only the defined tool names."
)

_MAX_EMPTY_RETRIES = 2
_MAX_LENGTH_RECOVERIES = 3
_MAX_GOAL_CONTINUATION_ROUNDS = 12
_MAX_INJECTION_CYCLES = 5
_MAX_INJECTIONS_PER_TURN = 3

_LENGTH_RECOVERY_PROMPT = (
    "Please continue from where you left off. "
    "Your previous response was truncated."
)
_EMPTY_RETRY_FINAL_MESSAGE = (
    "I encountered an issue generating a response. "
    "Please provide a brief summary or continuation."
)


@dataclass
class AgentRunSpec:
    initial_messages: list[dict[str, Any]]
    tools: ToolRegistry
    provider: LLMProvider
    max_iterations: int = 10
    model: str | None = None
    temperature: float = 0.7
    max_tokens: int = 4096
    hook: AgentHook | None = None
    session_key: str | None = None
    injection_callback: Callable[[], Awaitable[list[dict]]] | None = None
    checkpoint_callback: Callable[[dict[str, Any]], Awaitable[None]] | None = None
    governance_config: ContextGovernanceConfig | None = None
    goal_active_predicate: Callable[[], bool] | None = None
    goal_continue_message: str | None = None
    concurrent_tools: bool = True
    llm_timeout_s: float | None = None
    context_window_tokens: int | None = None
    goal_continuation_rounds: int = 0
    retry_wait_callback: Callable[[str], Awaitable[None]] | None = None
    progress_callback: Callable[..., Awaitable[None]] | None = None


@dataclass
class AgentRunResult:
    final_content: str | None
    messages: list[dict[str, Any]]
    tools_used: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    stop_reason: str = "completed"
    goal_continuation_rounds: int = 0

    @property
    def total_prompt_tokens(self) -> int:
        return self.usage.get("prompt_tokens", 0)

    @property
    def total_completion_tokens(self) -> int:
        return self.usage.get("completion_tokens", 0)


_MAX_ITERATIONS_FALLBACK = "Reached max iterations without a final response."


class AgentRunner:
    async def run(self, spec: AgentRunSpec) -> AgentRunResult:
        from step27.context import RequestContext, bind_request_context, reset_request_context

        messages = list(spec.initial_messages)
        tools_used: list[str] = []
        total_usage: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0}
        hook = spec.hook or AgentHook()

        req_ctx = RequestContext(session_key=spec.session_key)
        token = bind_request_context(req_ctx)

        run_ctx = AgentRunHookContext(messages=list(messages))
        await hook.before_run(run_ctx)

        try:
            result = await self._run_loop(
                spec, messages, tools_used, total_usage, hook,
            )
        except BaseException as exc:
            run_ctx.exception = exc
            await hook.on_error(run_ctx)
            raise
        else:
            spec.goal_continuation_rounds = result.goal_continuation_rounds
            run_ctx.final_content = result.final_content
            run_ctx.tools_used = list(result.tools_used)
            run_ctx.usage = dict(result.usage)
            run_ctx.stop_reason = result.stop_reason
            await hook.after_run(run_ctx)
            return result
        finally:
            reset_request_context(token)
            await hook.on_finally(run_ctx)

    @staticmethod
    def _resolve_gov_config(spec: AgentRunSpec) -> ContextGovernanceConfig:
        if spec.governance_config is not None:
            return spec.governance_config
        return ContextGovernanceConfig(
            tools=spec.tools,
            context_window_tokens=spec.context_window_tokens or 200_000,
            max_tokens=spec.max_tokens,
        )

    @staticmethod
    def _is_blank_text(text: str | None) -> bool:
        return not text or not text.strip()

    @staticmethod
    def _error_result(
        messages: list[dict[str, Any]],
        tools_used: list[str],
        total_usage: dict[str, int],
        response: LLMResponse,
        goal_continuation_rounds: int,
    ) -> AgentRunResult:
        return AgentRunResult(
            final_content=response.content or "",
            messages=messages,
            tools_used=tools_used,
            usage=total_usage,
            stop_reason="error",
            goal_continuation_rounds=goal_continuation_rounds,
        )

    async def _request_model(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        tools_defs: list[dict[str, Any]] | None,
        hook: AgentHook,
        iter_ctx: AgentHookContext,
    ) -> LLMResponse:
        timeout = spec.llm_timeout_s or 300.0
        wants_streaming = hook.wants_streaming()
        outer_timeout = max(300.0, timeout * 2) if wants_streaming else timeout

        async def _on_delta(text: str) -> None:
            iter_ctx.stream_content += text
            await hook.on_stream(iter_ctx, text)

        retry_kwargs: dict[str, Any] = {}
        if (
            spec.retry_wait_callback is not None
            and self._provider_method_accepts(
                spec.provider, "chat_stream_with_retry", "on_retry_wait"
            )
        ):
            retry_kwargs["on_retry_wait"] = spec.retry_wait_callback

        coro = spec.provider.chat_stream_with_retry(
            messages=messages, tools=tools_defs,
            model=spec.model, temperature=spec.temperature,
            max_tokens=spec.max_tokens, on_content_delta=_on_delta,
            **retry_kwargs,
        )
        try:
            return await asyncio.wait_for(coro, timeout=outer_timeout)
        except asyncio.TimeoutError:
            return LLMResponse(
                content="", finish_reason="error",
                usage={"prompt_tokens": 0, "completion_tokens": 0},
            )

    @staticmethod
    def _provider_method_accepts(provider: Any, method: str, kwarg: str) -> bool:
        """探测 provider 的 *method* 是否接受关键字参数 *kwarg*。

        对齐 step25 ``_drain_injections`` 的签名探测风格：mock provider（
        388 回归测试）与真实 provider 的签名宽度不一致，直接传参会
        TypeError，因此只在目标方法显式声明该参数或 ``**kwargs`` 时才传。
        """
        callable_obj = getattr(provider, method, None)
        if callable_obj is None:
            return False
        try:
            signature = inspect.signature(callable_obj)
        except (TypeError, ValueError):
            return True  # 内置/包装方法无法探测时假设宽容
        for param in signature.parameters.values():
            if param.kind is inspect.Parameter.VAR_KEYWORD:
                return True
            if param.name == kwarg:
                return True
        return False

    @staticmethod
    def _drop_malformed_tool_calls(tool_calls: list[Any]) -> list[Any]:
        return [tc for tc in tool_calls
                if hasattr(tc, 'name') and isinstance(tc.name, str) and tc.name.strip()]

    @staticmethod
    def _get_tool(spec: AgentRunSpec, name: str) -> Any:
        getter = getattr(spec.tools, 'get', None)
        return getter(name) if getter else None

    @staticmethod
    def _partition_tool_batches(
        spec: AgentRunSpec,
        tool_calls: list[Any],
    ) -> list[list[tuple[Any, Any]]]:
        batches: list[list[tuple[Any, Any]]] = []
        current_batch: list[tuple[Any, Any]] = []
        for tc in tool_calls:
            name = tc.name if hasattr(tc, 'name') else None
            tool = AgentRunner._get_tool(spec, name) if name else None
            is_safe = tool is not None and tool.concurrency_safe
            if is_safe:
                current_batch.append((tc, tool))
                if len(current_batch) >= _MAX_CONCURRENT_TOOLS:
                    batches.append(current_batch)
                    current_batch = []
            else:
                if current_batch:
                    batches.append(current_batch)
                    current_batch = []
                batches.append([(tc, tool)])
        if current_batch:
            batches.append(current_batch)
        return batches

    async def _execute_tool_batch(
        self,
        batch: list[tuple[Any, Any]],
        spec: AgentRunSpec,
        gov_config: ContextGovernanceConfig,
        hook: AgentHook,
        iter_ctx: AgentHookContext,
        tools_used: list[str],
    ) -> list[Any]:
        if len(batch) > 1 and spec.concurrent_tools:
            coros = [self._run_tool(tc, spec, gov_config, hook, iter_ctx, tools_used)
                     for tc, _ in batch]
            return await asyncio.gather(*coros)
        results = []
        for tc, _ in batch:
            r = await self._run_tool(tc, spec, gov_config, hook, iter_ctx, tools_used)
            results.append(r)
        return results

    async def _run_tool(
        self,
        tc: Any,
        spec: AgentRunSpec,
        gov_config: ContextGovernanceConfig,
        hook: AgentHook,
        iter_ctx: AgentHookContext,
        tools_used: list[str],
    ) -> Any:
        name = tc.name if hasattr(tc, 'name') else str(tc)
        tools_used.append(name)
        if hasattr(spec.tools, 'prepare_call'):
            tool, params, error = spec.tools.prepare_call(name, tc.arguments)
            if error:
                return str(error)
            result = await tool.execute(**params)
        else:
            result = await spec.tools.execute(name, **tc.arguments)
        await self._emit_tool_progress(spec, name, result)
        return _GOVERNOR.normalize_tool_result(
            gov_config, tc.id if hasattr(tc, 'id') else "", name, result,
        )

    async def _emit_tool_progress(
        self,
        spec: AgentRunSpec,
        name: str,
        result: Any,
    ) -> None:
        """低噪声进度：工具执行完成后发一条 ProgressEvent（tool_hint=False）。

        progress 回调由 loop 装配（``build_bus_progress_callback``）。签名
        兼容策略：只接受 ``content`` 的旧式回调（如部分 mock）也能工作，
        因此这里探测并仅传 content。
        """
        if spec.progress_callback is None:
            return
        snippet = str(result)[:80].replace("\n", " ")
        content = f"Ran tool {name}: {snippet}"
        try:
            signature = inspect.signature(spec.progress_callback)
        except (TypeError, ValueError):
            await spec.progress_callback(content)
            return
        if (
            any(
                p.kind is inspect.Parameter.VAR_KEYWORD
                for p in signature.parameters.values()
            )
            or "content" in signature.parameters
        ):
            await spec.progress_callback(content)
        else:
            await spec.progress_callback()

    def _build_goal_continue_message(self, spec: AgentRunSpec) -> dict[str, str]:
        return {
            "role": "user",
            "content": spec.goal_continue_message or (
                "You have an active sustained goal. "
                "Continue working toward the objective using your tools, "
                "or call update_goal with action='complete' if the work is done."
            ),
        }

    @staticmethod
    def _openai_tool_calls(tool_calls: list[Any]) -> list[dict[str, Any]]:
        return [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.name,
                    "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                },
            }
            for tc in tool_calls
        ]

    async def _emit_checkpoint(
        self,
        spec: AgentRunSpec,
        payload: dict[str, Any],
    ) -> None:
        callback = spec.checkpoint_callback
        if callback is not None:
            await callback(payload)

    async def _try_drain_injections(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        assistant_message: dict[str, Any] | None,
        injection_cycles: int,
        *,
        phase: str = "after error",
        iteration: int | None = None,
        allow_goal_continue: bool = False,
    ) -> tuple[bool, int]:
        """Drain pending follow-up messages.

        Returns ``(should_continue, updated_cycles)``.  When injections are
        found (and *injection_cycles* is under ``_MAX_INJECTION_CYCLES``) they
        are appended to *messages* and ``(True, cycles + 1)`` is returned so
        the caller keeps the iteration loop alive.  With no injections and
        *allow_goal_continue*, an active sustained goal falls back to a goal
        continuation message (does not consume an injection cycle).
        *assistant_message* is appended only when the turn, in fact, continues.
        """
        injections: list[dict[str, Any]] = []
        real_injection = False
        if injection_cycles < _MAX_INJECTION_CYCLES:
            injections = await self._drain_injections(spec)
            real_injection = bool(injections)
        if (
            not injections
            and allow_goal_continue
            and assistant_message is not None
            and spec.goal_active_predicate is not None
            and spec.goal_active_predicate()
            and spec.goal_continuation_rounds < _MAX_GOAL_CONTINUATION_ROUNDS
        ):
            injections = [self._build_goal_continue_message(spec)]
        if not injections:
            return False, injection_cycles
        if real_injection:
            injection_cycles += 1
        if assistant_message is not None:
            messages.append(assistant_message)
            if iteration is not None:
                await self._emit_checkpoint(
                    spec,
                    {
                        "phase": "final_response",
                        "iteration": iteration,
                        "model": spec.model,
                        "assistant_message": assistant_message,
                        "completed_tool_results": [],
                        "pending_tool_calls": [],
                    },
                )
        self._append_injected_messages(messages, injections)
        if real_injection:
            logger.info(
                "Injected %d follow-up message(s) %s (%d/%d)",
                len(injections), phase, injection_cycles, _MAX_INJECTION_CYCLES,
            )
        else:
            logger.info("Injected sustained-goal continuation %s", phase)
            spec.goal_continuation_rounds += 1
        return True, injection_cycles

    async def _drain_injections(self, spec: AgentRunSpec) -> list[dict[str, Any]]:
        """Drain follow-up messages via ``spec.injection_callback``.

        Returns normalized user messages (capped by ``_MAX_INJECTIONS_PER_TURN``),
        or an empty list when there is nothing to inject.  The callback may
        accept a ``limit`` keyword argument (probed via ``inspect.signature``).
        Blank / malformed items are filtered out; items beyond the cap are
        logged rather than silently lost.
        """
        if spec.injection_callback is None:
            return []
        try:
            signature = inspect.signature(spec.injection_callback)
            accepts_limit = (
                "limit" in signature.parameters
                or any(p.kind is inspect.Parameter.VAR_KEYWORD
                       for p in signature.parameters.values())
            )
            items = (
                await spec.injection_callback(limit=_MAX_INJECTIONS_PER_TURN)
                if accepts_limit else await spec.injection_callback()
            )
        except Exception:
            logger.exception("injection_callback failed")
            return []
        if not items:
            return []
        injected_messages: list[dict[str, Any]] = []
        for item in items:
            if item is None:
                continue
            if isinstance(item, dict) and item.get("role") == "user" and "content" in item:
                if self._has_injection_content(item.get("content")):
                    injected_messages.append(item)
                    continue
            if isinstance(item, dict):
                continue
            content = getattr(item, "content") if hasattr(item, "content") else str(item)
            if self._has_injection_content(content):
                injected_messages.append({"role": "user", "content": content})
        if len(injected_messages) > _MAX_INJECTIONS_PER_TURN:
            dropped = len(injected_messages) - _MAX_INJECTIONS_PER_TURN
            logger.warning(
                "injection_callback returned %d messages, capping to %d (%d dropped)",
                len(injected_messages), _MAX_INJECTIONS_PER_TURN, dropped,
            )
            injected_messages = injected_messages[:_MAX_INJECTIONS_PER_TURN]
        return injected_messages

    @staticmethod
    def _has_injection_content(content: Any) -> bool:
        if content is None:
            return False
        if isinstance(content, str):
            return bool(content.strip())
        if isinstance(content, list):
            return bool(content)
        return True

    @staticmethod
    def _append_injected_messages(
        messages: list[dict[str, Any]],
        injected: list[dict[str, Any]],
    ) -> None:
        for msg in injected:
            if msg["role"] != "user":
                messages.append(msg)
                continue
            if messages and messages[-1]["role"] == "user":
                messages[-1]["content"] += "\n" + msg["content"]
            else:
                messages.append(msg)

    async def _run_loop(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        tools_used: list[str],
        total_usage: dict[str, int],
        hook: AgentHook,
    ) -> AgentRunResult:
        compacted_tool_call_ids: set[str] = set()
        gov_config = self._resolve_gov_config(spec)
        empty_retries = 0
        length_recovery_count = 0
        injection_cycles = 0

        for iteration in range(spec.max_iterations):
            messages = _GOVERNOR.prepare_for_model(
                gov_config, messages, compacted_tool_call_ids,
            )

            iter_ctx = AgentHookContext(
                iteration=iteration,
                messages=messages,
                session_key=spec.session_key,
            )
            await hook.before_iteration(iter_ctx)

            tools_defs = spec.tools.get_definitions() or None

            response = await self._request_model(
                spec, messages, tools_defs, hook, iter_ctx,
            )
            self._accumulate_usage(total_usage, response)
            iter_ctx.response = response
            iter_ctx.usage = dict(total_usage)
            await hook.on_stream_end(iter_ctx)

            if response.finish_reason == "error":
                iter_ctx.final_content = response.content or ""
                iter_ctx.stop_reason = "error"
                await hook.after_iteration(iter_ctx)
                return self._error_result(
                    messages, tools_used, total_usage,
                    response, spec.goal_continuation_rounds,
                )

            if response.tool_calls and response.finish_reason == "tool_calls":
                valid_calls = self._drop_malformed_tool_calls(response.tool_calls)
                if not valid_calls:
                    messages.append({
                        "role": "user", "content": _MALFORMED_TOOL_RETRY_MESSAGE,
                    })
                    continue

                filtered = LLMResponse(
                    content=response.content, tool_calls=valid_calls,
                    finish_reason=response.finish_reason, usage=response.usage,
                )
                assistant_msg = self._build_assistant_message(filtered)
                messages.append(assistant_msg)
                iter_ctx.tool_calls = list(valid_calls)

                await self._emit_checkpoint(
                    spec,
                    {
                        "phase": "awaiting_tools",
                        "iteration": iteration,
                        "model": spec.model,
                        "assistant_message": assistant_msg,
                        "completed_tool_results": [],
                        "pending_tool_calls": self._openai_tool_calls(valid_calls),
                    },
                )

                batches = self._partition_tool_batches(spec, valid_calls)
                completed_tool_results: list[dict[str, Any]] = []
                for batch in batches:
                    results = await self._execute_tool_batch(
                        batch, spec, gov_config, hook, iter_ctx, tools_used,
                    )
                    for used_tc, result in zip([tc for tc, _ in batch], results):
                        tool_msg = {
                            "role": "tool", "tool_call_id": used_tc.id,
                            "name": used_tc.name, "content": str(result),
                        }
                        messages.append(tool_msg)
                        completed_tool_results.append(tool_msg)
                        iter_ctx.tool_results.append(str(result))

                await self._emit_checkpoint(
                    spec,
                    {
                        "phase": "tools_completed",
                        "iteration": iteration,
                        "model": spec.model,
                        "assistant_message": assistant_msg,
                        "completed_tool_results": completed_tool_results,
                        "pending_tool_calls": [],
                    },
                )

                iter_ctx.stop_reason = "tool_calls"
                await hook.after_iteration(iter_ctx)

                _, injection_cycles = await self._try_drain_injections(
                    spec, messages, None, injection_cycles,
                    phase="after tool execution",
                )
                continue

            clean = response.content or ""

            if self._is_blank_text(clean):
                if empty_retries < _MAX_EMPTY_RETRIES:
                    empty_retries += 1
                    continue

                messages.append({
                    "role": "user", "content": _EMPTY_RETRY_FINAL_MESSAGE,
                })
                response = await self._request_model(
                    spec, messages, None, hook, iter_ctx,
                )
                self._accumulate_usage(total_usage, response)
                iter_ctx.response = response
                iter_ctx.usage = dict(total_usage)
                await hook.on_stream_end(iter_ctx)
                if response.finish_reason == "error":
                    iter_ctx.final_content = response.content or ""
                    iter_ctx.stop_reason = "error"
                    await hook.after_iteration(iter_ctx)
                    return self._error_result(
                        messages, tools_used, total_usage,
                        response, spec.goal_continuation_rounds,
                    )
                clean = response.content or ""

            if response.finish_reason == "length" and not self._is_blank_text(clean):
                if length_recovery_count < _MAX_LENGTH_RECOVERIES:
                    assistant_msg = self._build_assistant_message(response)
                    messages.append(assistant_msg)
                    messages.append({
                        "role": "user", "content": _LENGTH_RECOVERY_PROMPT,
                    })
                    length_recovery_count += 1
                    continue

            assistant_message = {"role": "assistant", "content": clean}
            should_continue, injection_cycles = await self._try_drain_injections(
                spec, messages, assistant_message, injection_cycles,
                phase="after final response", iteration=iteration,
                allow_goal_continue=True,
            )
            if should_continue:
                await hook.after_iteration(iter_ctx)
                continue
            messages.append(assistant_message)
            await self._emit_checkpoint(
                spec,
                {
                    "phase": "final_response",
                    "iteration": iteration,
                    "model": spec.model,
                    "assistant_message": messages[-1],
                    "completed_tool_results": [],
                    "pending_tool_calls": [],
                },
            )
            iter_ctx.final_content = clean
            iter_ctx.stop_reason = response.finish_reason
            await hook.after_iteration(iter_ctx)

            return AgentRunResult(
                final_content=clean,
                messages=messages,
                tools_used=tools_used,
                usage=total_usage,
                stop_reason=response.finish_reason,
                goal_continuation_rounds=spec.goal_continuation_rounds,
            )

        return AgentRunResult(
            final_content=_MAX_ITERATIONS_FALLBACK,
            messages=messages,
            tools_used=tools_used,
            usage=total_usage,
            stop_reason="max_iterations",
            goal_continuation_rounds=spec.goal_continuation_rounds,
        )

    @staticmethod
    def _build_assistant_message(response: LLMResponse) -> dict[str, Any]:
        tool_calls_raw = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.name,
                    "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                },
            }
            for tc in response.tool_calls
        ]
        return {
            "role": "assistant",
            "content": response.content,
            "tool_calls": tool_calls_raw,
        }

    @staticmethod
    def _accumulate_usage(total: dict[str, int], response: LLMResponse) -> None:
        if response.usage:
            for key in ("prompt_tokens", "completion_tokens"):
                val = response.usage.get(key, 0)
                if val:
                    total[key] = total.get(key, 0) + val
