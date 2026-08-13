from __future__ import annotations

import asyncio
import inspect
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from step33.governance import ContextGovernanceConfig, ContextGovernor
from step33.hook import AgentHook, AgentHookContext, AgentRunHookContext
from step33.llm import LLMResponse
from step33.provider import LLMProvider
from step33.tool import Tool, ToolRegistry


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

# step32（A8）：欠费/配额不足类错误（HTTP 402 或 billing 语义 token）不会被
# 重试清除，直接给用户可行动的文案（对齐 nanobot runner ``_ARREARAGE_ERROR_MESSAGE``）。
_ARREARAGE_ERROR_MESSAGE = (
    "Account balance insufficient or API key in arrears. "
    "Please top up your balance or check your API key, then try again."
)

_LENGTH_RECOVERY_PROMPT = (
    "Please continue from where you left off. "
    "Your previous response was truncated."
)
_EMPTY_RETRY_FINAL_MESSAGE = (
    "I encountered an issue generating a response. "
    "Please provide a brief summary or continuation."
)

# step32：finalization 提示词——max_iterations / 畸形工具调用重试时，
# 追加一条 user 消息让模型基于已有对话给出最终答案（对齐 nanobot
# ``utils/runtime.py`` 的 BUDGET_EXHAUSTED_FINALIZATION_PROMPT /
# FINALIZATION_RETRY_PROMPT）。
_BUDGET_EXHAUSTED_FINALIZATION_PROMPT = (
    "The tool-call budget for this turn is exhausted. Based only on the "
    "conversation and tool results above, provide a concise final response to "
    "the user. Do not call or request tools. Do not claim the task is complete "
    "unless the evidence above clearly shows it is complete. State what was "
    "done, what remains, and the best next step if anything is incomplete."
)
_FINALIZATION_RETRY_PROMPT = (
    "Please provide your response to the user based on the conversation above."
)
# step32：error / empty 终止时的默认文案（对齐 nanobot
# ``_DEFAULT_ERROR_MESSAGE`` / ``EMPTY_FINAL_RESPONSE_MESSAGE``）。
_DEFAULT_ERROR_MESSAGE = "Sorry, I encountered an error calling the AI model."
_EMPTY_FINAL_RESPONSE_MESSAGE = (
    "I completed the tool steps but couldn't produce a final answer. "
    "Please try again or narrow the task."
)


@dataclass
class AgentRunSpec:
    """一次 agent 运行的完整参数（step29 起新增 request_context / workspace_scope）。

    Attributes:
        initial_messages: 初始消息列表（含 system）。
        tools: 工具注册表。
        provider: LLM provider。
        max_iterations: 最大迭代轮数。
        request_context: 富 RequestContext（含 workspace/runtime/turn_id）；
            None 时 runner 回退到最小 ``RequestContext(session_key=...)``。
        workspace_scope: 本 turn 的 WorkspaceScope；绑定到 ContextVar 供工具
            查询（``current_tool_workspace``）；None 时不绑定。
        finalize_on_max_iterations: max_iterations 边界是否产出收尾响应。
            False 表示预算边界由隐形续跑接管（step29 A12）：runner 不再
            生成用户可见 fallback 文案，loop 负责排班下一片并剥掉合成消息。
            （step29 仅开关语义；step32 将补充"收尾 = 一次无工具 LLM 调用"。）
    """

    initial_messages: list[dict[str, Any]]
    tools: ToolRegistry
    provider: LLMProvider
    max_iterations: int = 10
    model: str | None = None
    temperature: float = 0.7
    max_tokens: int = 4096
    hook: AgentHook | None = None
    session_key: str | None = None
    request_context: Any | None = None
    workspace_scope: Any | None = None
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
    finalize_on_max_iterations: bool = True
    # step32（A8）：可定制文案——error 终止时用 ``error_message``（缺省回退
    # 到响应内容，欠费场景自动换 ``_ARREARAGE_ERROR_MESSAGE``）；
    # max_iterations 收尾用 ``max_iterations_message``（缺省
    # ``_MAX_ITERATIONS_FALLBACK``）。
    error_message: str | None = None
    max_iterations_message: str | None = None


@dataclass
class AgentRunResult:
    """一次 agent 运行的结果（step32 新增 error / had_injections 字段）。

    Attributes:
        final_content: 最终响应文本；None 表示未产出用户可见内容（如
            finalize_on_max_iterations=False 时隐形续跑接管）。
        messages: 完整消息列表（含 system + 历史 + 本 turn 新增）。
        tools_used: 本 turn 调用过的工具名列表。
        usage: 累计 token 使用量。
        stop_reason: 终止原因（completed / error / max_iterations /
            empty_final_response / tool_calls 等）。
        goal_continuation_rounds: 目标续跑轮数。
        error: 错误文案（error 终止时设置）；None 表示无错误。
        had_injections: 本 turn 是否发生过注入排空（pending 消息注入）。
    """

    final_content: str | None
    messages: list[dict[str, Any]]
    tools_used: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    stop_reason: str = "completed"
    goal_continuation_rounds: int = 0
    error: str | None = None
    had_injections: bool = False

    @property
    def total_prompt_tokens(self) -> int:
        return self.usage.get("prompt_tokens", 0)

    @property
    def total_completion_tokens(self) -> int:
        return self.usage.get("completion_tokens", 0)


_MAX_ITERATIONS_FALLBACK = "Reached max iterations without a final response."


class AgentRunner:
    async def run(self, spec: AgentRunSpec) -> AgentRunResult:
        from step33.context import RequestContext, bind_request_context, reset_request_context
        from step33.security.workspace_access import bind_workspace_scope, reset_workspace_scope

        messages = list(spec.initial_messages)
        tools_used: list[str] = []
        total_usage: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0}
        hook = spec.hook or AgentHook()

        # step29：优先绑定调用方（loop/subagent）提供的富 RequestContext，
        # 缺省时回退到仅含 session_key 的最小上下文（保持旧行为兼容）。
        req_ctx = spec.request_context or RequestContext(session_key=spec.session_key)
        token = bind_request_context(req_ctx)
        # step29：本 turn 的 workspace scope 一并绑定，工具内可通过
        # ``current_tool_workspace()`` 查询到真实的项目根与受限状态。
        ws_token = (
            bind_workspace_scope(spec.workspace_scope)
            if spec.workspace_scope is not None
            else None
        )

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
            if ws_token is not None:
                reset_workspace_scope(ws_token)
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
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        tools_used: list[str],
        total_usage: dict[str, int],
        response: LLMResponse,
        goal_continuation_rounds: int,
        had_injections: bool = False,
    ) -> AgentRunResult:
        # step32（A8）：文案优先级——自定义 error_message > 欠费识别
        # （is_arrearage_response，HTTP 402 / billing token）> 响应原文。
        if spec.error_message is not None:
            content = spec.error_message
        elif (
            spec.provider.is_arrearage_response(response)
            if hasattr(spec.provider, "is_arrearage_response")
            else False
        ):
            content = _ARREARAGE_ERROR_MESSAGE
        else:
            content = response.content or ""
        return AgentRunResult(
            final_content=content,
            messages=messages,
            tools_used=tools_used,
            usage=total_usage,
            stop_reason="error",
            goal_continuation_rounds=goal_continuation_rounds,
            error=content,
            had_injections=had_injections,
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
            response = await asyncio.wait_for(coro, timeout=outer_timeout)
        except asyncio.TimeoutError:
            return LLMResponse(
                content="", finish_reason="error",
                usage={"prompt_tokens": 0, "completion_tokens": 0},
            )
        # step32（A8）：usage 缺失时按文本长度估算（~4 字符/token），保证
        # usage 累积与预算簿记不因 provider 不给 usage 而断档。
        if not response.usage:
            response = LLMResponse(
                content=response.content,
                tool_calls=response.tool_calls,
                finish_reason=response.finish_reason,
                usage=self._estimate_usage(messages, response),
                retry_after=response.retry_after,
                reasoning_content=response.reasoning_content,
                thinking_blocks=response.thinking_blocks,
                error_status_code=response.error_status_code,
                error_kind=response.error_kind,
                error_type=response.error_type,
                error_code=response.error_code,
                error_retry_after_s=response.error_retry_after_s,
                error_should_retry=response.error_should_retry,
            )
        return response

    @staticmethod
    def _estimate_usage(
        messages: list[dict[str, Any]], response: LLMResponse
    ) -> dict[str, int]:
        """按文本长度估算 prompt / completion tokens（对齐 nanobot 估算回退）。

        启发式：约 4 个字符 ≈ 1 token；估算值仅用于预算簿记，不精确。
        """
        prompt_chars = sum(len(str(m.get("content", ""))) for m in messages)
        completion_chars = len(response.content or "")
        return {
            "prompt_tokens": max(1, prompt_chars // 4),
            "completion_tokens": max(1, completion_chars // 4),
        }

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
        # step29：带隐藏标记（HIDDEN_HISTORY_META）的注入行不并入上一行
        # user（否则标记丢失且角色交替语义被破坏），独立追加。
        from step33.session.history_visibility import is_hidden_history_message

        for msg in injected:
            if msg["role"] != "user":
                messages.append(msg)
                continue
            if (
                messages
                and messages[-1]["role"] == "user"
                and not is_hidden_history_message(msg)
                and not is_hidden_history_message(messages[-1])
            ):
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
        had_injections = False

        for iteration in range(spec.max_iterations):
            # step32：governance 异常保护——prepare_for_model 可能因畸形
            # 历史消息抛异常，此时逐步 strip/repair，全部失败才用原始 messages
            # （对齐 nanobot runner ``_run_core`` 356-386 行）。
            try:
                messages_for_model = _GOVERNOR.prepare_for_model(
                    gov_config, messages, compacted_tool_call_ids,
                )
            except Exception:
                logger.exception(
                    "Context governance failed on turn %d for %s; applying minimal repair",
                    iteration, spec.session_key or "default",
                )
                try:
                    messages_for_model = ContextGovernor.strip_placeholder_assistant_messages(
                        messages
                    )
                    messages_for_model = ContextGovernor.strip_malformed_tool_calls(
                        messages_for_model
                    )
                    messages_for_model = ContextGovernor.drop_orphan_tool_results(
                        messages_for_model
                    )
                    messages_for_model = ContextGovernor.backfill_missing_tool_results(
                        messages_for_model
                    )
                except Exception:
                    messages_for_model = messages

            iter_ctx = AgentHookContext(
                iteration=iteration,
                messages=messages,
                session_key=spec.session_key,
            )
            await hook.before_iteration(iter_ctx)

            tools_defs = spec.tools.get_definitions() or None

            response = await self._request_model(
                spec, messages_for_model, tools_defs, hook, iter_ctx,
            )
            self._accumulate_usage(total_usage, response)
            iter_ctx.response = response
            iter_ctx.usage = dict(total_usage)
            # step32：对齐 nanobot——模型响应后先别关流（工具执行/重试/注入
            # 可能紧随其后），resuming=True 让流保持存活。
            await hook.on_stream_end(iter_ctx, resuming=True)

            if response.finish_reason == "error":
                iter_ctx.final_content = response.content or ""
                iter_ctx.stop_reason = "error"
                # error 终止：流到此真正结束。
                await hook.on_stream_end(iter_ctx, resuming=False)
                await hook.after_iteration(iter_ctx)
                # step32：error 后也尝试排空注入——有 pending 消息时
                # continue 让注入被消费，否则才返回 error（对齐 nanobot
                # runner ``_run_core`` 608-614 行）。
                should_continue, injection_cycles = await self._try_drain_injections(
                    spec, messages, None, injection_cycles,
                    phase="after LLM error",
                )
                if should_continue:
                    had_injections = True
                    continue
                return self._error_result(
                    spec, messages, tools_used, total_usage,
                    response, spec.goal_continuation_rounds,
                    had_injections=had_injections,
                )

            # step32（A8）：工具只在 ``should_execute_tools`` 下执行——
            # ``refusal`` / ``content_filter`` / ``error`` 等终止里网关注入
            # 的调用不可信，直接丢弃，仅把文本当最终内容（对齐 nanobot
            # ``LLMResponse.should_execute_tools``，issue #3220）。
            if response.has_tool_calls and not response.should_execute_tools:
                logger.warning(
                    "Discarding %d tool call(s) under finish_reason=%r",
                    len(response.tool_calls), response.finish_reason,
                )
                response = LLMResponse(
                    content=response.content,
                    finish_reason="stop",
                    usage=response.usage,
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

                drained, injection_cycles = await self._try_drain_injections(
                    spec, messages, None, injection_cycles,
                    phase="after tool execution",
                )
                if drained:
                    had_injections = True
                continue

            clean = response.content or ""

            if self._is_blank_text(clean):
                if empty_retries < _MAX_EMPTY_RETRIES:
                    empty_retries += 1
                    # 空响应重试：先关流，避免上一段空流悬挂。
                    await hook.on_stream_end(iter_ctx, resuming=False)
                    continue

                # step32：对齐 nanobot——空响应重试耗尽后不再发额外请求，
                # 直接用 _EMPTY_FINAL_RESPONSE_MESSAGE 作为最终内容，
                # 并尝试排空注入（对齐 nanobot runner ``_run_core`` 616-632 行）。
                final_content = _EMPTY_FINAL_RESPONSE_MESSAGE
                messages.append({"role": "assistant", "content": final_content})
                iter_ctx.final_content = final_content
                iter_ctx.stop_reason = "empty_final_response"
                await hook.on_stream_end(iter_ctx, resuming=False)
                await hook.after_iteration(iter_ctx)
                should_continue, injection_cycles = await self._try_drain_injections(
                    spec, messages, None, injection_cycles,
                    phase="after empty response",
                )
                if should_continue:
                    had_injections = True
                    continue
                return AgentRunResult(
                    final_content=final_content,
                    messages=messages,
                    tools_used=tools_used,
                    usage=total_usage,
                    stop_reason="empty_final_response",
                    goal_continuation_rounds=spec.goal_continuation_rounds,
                    error=final_content,
                    had_injections=had_injections,
                )

            if response.finish_reason == "length" and not self._is_blank_text(clean):
                if length_recovery_count < _MAX_LENGTH_RECOVERIES:
                    assistant_msg = self._build_assistant_message(response)
                    messages.append(assistant_msg)
                    messages.append({
                        "role": "user", "content": _LENGTH_RECOVERY_PROMPT,
                    })
                    length_recovery_count += 1
                    # 截断恢复：流保持存活（恢复请求会继续发内容）。
                    await hook.on_stream_end(iter_ctx, resuming=True)
                    continue

            assistant_message = {"role": "assistant", "content": clean}
            should_continue, injection_cycles = await self._try_drain_injections(
                spec, messages, assistant_message, injection_cycles,
                phase="after final response", iteration=iteration,
                allow_goal_continue=True,
            )
            # step32：对齐 nanobot——先查注入再决定关流：还有注入续跑时
            # 保持流存活（resuming=True），否则正常收尾（resuming=False）。
            await hook.on_stream_end(iter_ctx, resuming=should_continue)
            if should_continue:
                had_injections = True
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
                had_injections=had_injections,
            )

        # step32：max_iterations 边界——先排空注入，再尝试无工具 finalization
        # 请求让模型给最终答案，失败才用 fallback 文案（对齐 nanobot runner
        # ``_run_core`` 655-678 行）。
        stop_reason = "max_iterations"
        # 先排空剩余注入，让 finalization 请求能看到所有已知后续。
        drained, injection_cycles = await self._try_drain_injections(
            spec, messages, None, injection_cycles,
            phase="after max_iterations",
        )
        if drained:
            had_injections = True
        await hook.on_stream_end(iter_ctx, resuming=not spec.finalize_on_max_iterations)

        final_content: str | None = None
        if spec.finalize_on_max_iterations:
            final_content = await self._try_finalize_after_max_iterations(
                spec, hook, messages, total_usage,
            )
            if final_content is None:
                # finalization 失败时用可定制 fallback 文案。
                final_content = spec.max_iterations_message or _MAX_ITERATIONS_FALLBACK
            messages.append({"role": "assistant", "content": final_content})
        # finalize_on_max_iterations=False 时 final_content 保持 None（隐形续跑接管），
        # 不追加合成 assistant 消息。
        return AgentRunResult(
            final_content=final_content,
            messages=messages,
            tools_used=tools_used,
            usage=total_usage,
            stop_reason=stop_reason,
            goal_continuation_rounds=spec.goal_continuation_rounds,
            had_injections=had_injections,
        )

    async def _request_no_tools(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        hook: AgentHook,
        iter_ctx: AgentHookContext,
    ) -> LLMResponse:
        """发送不带工具定义的请求，强制模型给出最终文本答案。

        用于 max_iterations finalization：工具预算已耗尽，让模型基于
        已有对话和工具结果直接总结，不再允许调用工具（对齐 nanobot
        runner ``_request_no_tools`` 983-989 行）。

        Args:
            spec: 本次运行的参数。
            messages: 给模型的消息列表。
            hook: turn 级 hook（用于流式回调）。
            iter_ctx: 当前迭代上下文。

        Returns:
            LLM 响应。
        """
        return await self._request_model(spec, messages, None, hook, iter_ctx)

    async def _try_finalize_after_max_iterations(
        self,
        spec: AgentRunSpec,
        hook: AgentHook,
        messages: list[dict[str, Any]],
        total_usage: dict[str, int],
    ) -> str | None:
        """max_iterations 时尝试一次无工具请求让模型给出最终答案。

        在工具调用预算耗尽后，追加一条 finalization 提示消息，发一次
        ``tools=None`` 的请求。如果模型返回纯文本（无 tool_calls），
        用其内容作为最终答案；如果返回 error 或仍含 tool_calls，
        返回 None 让调用方走 fallback 文案（对齐 nanobot runner
        ``_try_finalize_after_max_iterations`` 942-981 行）。

        Args:
            spec: 本次运行的参数。
            hook: turn 级 hook。
            messages: 当前消息列表（会被修改：追加 finalization 提示和
                成功时的 assistant 消息）。
            total_usage: 累计 token 使用量（finalization 请求的 usage
                会累计进去）。

        Returns:
            模型给出的最终文本；失败时返回 None。
        """
        finalization_messages = list(messages)
        finalization_messages.append({
            "role": "user",
            "content": _BUDGET_EXHAUSTED_FINALIZATION_PROMPT,
        })
        iter_ctx = AgentHookContext(
            iteration=spec.max_iterations,
            messages=finalization_messages,
            session_key=spec.session_key,
        )
        try:
            response = await self._request_no_tools(
                spec, finalization_messages, hook, iter_ctx,
            )
        except Exception:
            logger.exception(
                "Finalization request failed for %s",
                spec.session_key or "default",
            )
            return None
        self._accumulate_usage(total_usage, response)
        if response.finish_reason == "error":
            logger.warning(
                "Finalization request returned error for %s",
                spec.session_key or "default",
            )
            return None
        if response.has_tool_calls:
            # 模型在无工具请求中仍返回 tool_calls——视为失败，走 fallback。
            logger.warning(
                "Finalization request returned tool_calls for %s; using fallback",
                spec.session_key or "default",
            )
            return None
        clean = (response.content or "").strip()
        if not clean:
            return None
        # 成功：把 finalization 提示和模型回答写入 messages（持久化）。
        messages.append({
            "role": "user",
            "content": _BUDGET_EXHAUSTED_FINALIZATION_PROMPT,
        })
        messages.append({"role": "assistant", "content": clean})
        return clean

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
