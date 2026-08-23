from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from step65.governance import ContextGovernanceConfig, ContextGovernor
from step65.helpers import (
    estimate_message_tokens,
    estimate_prompt_tokens_chain,
    extract_reasoning,
    IncrementalThinkExtractor,
    repeated_external_lookup_error,
    repeated_workspace_violation_error,
    strip_think,
)
from step65.hook import AgentHook, AgentHookContext, AgentRunHookContext
from step65.llm import LLMResponse
from step65.provider import LLMProvider
from step65.tool import Tool, ToolRegistry, ToolResult


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
# step64：模型错误时持久化到历史的占位符（对齐 nanobot）
_PERSISTED_MODEL_ERROR_PLACEHOLDER = "[Assistant reply unavailable due to model error.]"


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
    goal_continue_message: str | Callable[[], str | None] | None = None
    concurrent_tools: bool = True
    fail_on_tool_error: bool = False
    llm_timeout_s: float | None = None
    context_window_tokens: int | None = None
    goal_continuation_rounds: int = 0
    retry_wait_callback: Callable[[str], Awaitable[None]] | None = None
    progress_callback: Callable[..., Awaitable[None]] | None = None
    stream_progress_deltas: bool = True
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
    tool_events: list[dict[str, str]] = field(default_factory=list)

    @property
    def total_prompt_tokens(self) -> int:
        return self.usage.get("prompt_tokens", 0)

    @property
    def total_completion_tokens(self) -> int:
        return self.usage.get("completion_tokens", 0)


_MAX_ITERATIONS_FALLBACK = "Reached max iterations without a final response."


class AgentRunner:
    @staticmethod
    def _merge_message_content(left: Any, right: Any) -> str | list[dict[str, Any]]:
        """合并两条消息的 content（step64：对齐 nanobot）。

        字符串+字符串用换行合并；多模态 content（list[dict]）拼接 block。

        Args:
            left: 左侧 content。
            right: 右侧 content。

        Returns:
            合并后的 content。
        """
        if isinstance(left, str) and isinstance(right, str):
            return f"{left}\n\n{right}" if left else right

        def _to_blocks(value: Any) -> list[dict[str, Any]]:
            if isinstance(value, list):
                return [
                    item if isinstance(item, dict) else {"type": "text", "text": str(item)}
                    for item in value
                ]
            if value is None:
                return []
            return [{"type": "text", "text": str(value)}]

        return _to_blocks(left) + _to_blocks(right)

    def _build_request_kwargs(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        """构建 LLM provider 请求参数（step64：从 _request_model 提取）。

        Args:
            spec: 运行参数。
            messages: 消息列表。
            tools: 工具定义列表。

        Returns:
            provider 调用的 kwargs 字典。
        """
        return {
            "messages": messages,
            "tools": tools,
            "model": spec.model,
            "temperature": spec.temperature,
            "max_tokens": spec.max_tokens,
        }

    @staticmethod
    def _append_final_message(messages: list[dict[str, Any]], content: str | None) -> None:
        """追加最终 assistant 消息，避免重复（step64：对齐 nanobot）。

        如果最后一条消息已经是 assistant 且无 tool_calls，则替换其 content；
        否则追加新消息。

        Args:
            messages: 消息列表（原地修改）。
            content: 最终内容，空值时不操作。
        """
        if not content:
            return
        if (
            messages
            and messages[-1].get("role") == "assistant"
            and not messages[-1].get("tool_calls")
        ):
            if messages[-1].get("content") == content:
                return
            messages[-1] = {"role": "assistant", "content": content}
            return
        messages.append({"role": "assistant", "content": content})

    @staticmethod
    def _append_model_error_placeholder(messages: list[dict[str, Any]]) -> None:
        """追加模型错误占位符消息（step64：对齐 nanobot）。

        当最后一条 assistant 消息已有 content 时不重复追加。

        Args:
            messages: 消息列表（原地修改）。
        """
        if messages and messages[-1].get("role") == "assistant" and not messages[-1].get("tool_calls"):
            return
        messages.append({"role": "assistant", "content": _PERSISTED_MODEL_ERROR_PLACEHOLDER})

    async def run(self, spec: AgentRunSpec) -> AgentRunResult:
        from step65.context import RequestContext, bind_request_context, reset_request_context
        from step65.security.workspace_access import bind_workspace_scope, reset_workspace_scope
        from step65.tools.file_state import FileStates, bind_file_states, reset_file_states

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
        # step39：绑定文件读写状态 ContextVar，供 read_file/write_file 等
        # 工具查询 read-before-edit 警告与 read dedup。每次 run 独立实例。
        file_state_token = bind_file_states(FileStates())

        run_ctx = AgentRunHookContext(messages=list(messages))
        await hook.before_run(run_ctx)

        try:
            result = await self._run_loop(
                spec, messages, tools_used, total_usage, hook,
            )
        except asyncio.CancelledError as exc:
            run_ctx.exception = exc
            run_ctx.stop_reason = "cancelled"
            raise  # step64：CancelledError 不调 on_error
        except Exception as exc:
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
            # step39：先重置 file_state，再重置 workspace/request_context
            # （与绑定顺序相反，保证嵌套正确）。
            reset_file_states(file_state_token)
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
        tool_events: list[dict[str, str]] | None = None,
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
            tool_events=tool_events or [],
        )

    async def _request_model(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        tools_defs: list[dict[str, Any]] | None,
        hook: AgentHook,
        iter_ctx: AgentHookContext,
        *,
        malformed_retry: bool = False,
    ) -> LLMResponse:
        # step37：对齐 nanobot 超时逻辑——llm_timeout_s=None 时读环境变量
        # NANOBOT_LLM_TIMEOUT_S（默认 300s）；llm_timeout_s<=0 表示禁用超时。
        # 原 ``spec.llm_timeout_s or 300.0`` 有 bug：0.0 是 falsy，会被覆盖为 300。
        timeout_s: float | None = spec.llm_timeout_s
        if timeout_s is None:
            raw = os.environ.get("NANOBOT_LLM_TIMEOUT_S", "300").strip()
            try:
                timeout_s = float(raw)
            except (TypeError, ValueError):
                timeout_s = 300.0
        if timeout_s is not None and timeout_s <= 0:
            timeout_s = None  # 0.0 表示禁用超时

        wants_streaming = hook.wants_streaming()
        # 流式请求超时加倍（至少 300s），对齐 nanobot。
        outer_timeout = (
            max(300.0, timeout_s * 2)
            if wants_streaming and timeout_s is not None
            else timeout_s
        )

        # step64：流式 thinking 提取状态（仅非流式 + progress_callback 时启用）
        stream_buf = ""
        think_extractor = (
            IncrementalThinkExtractor()
            if spec.stream_progress_deltas
            and not wants_streaming
            and spec.progress_callback is not None
            else None
        )
        reasoning_open = False

        async def _on_delta(text: str) -> None:
            nonlocal stream_buf, reasoning_open
            if not text:
                return
            if think_extractor is not None:
                stream_buf += text
                # 提取 thinking 并 emit
                if await think_extractor.feed(stream_buf, hook.emit_reasoning):
                    iter_ctx.streamed_reasoning = True
                    reasoning_open = True
                # 计算非 think 内容增量
                prev_clean = strip_think(stream_buf[:-len(text)])
                new_clean = strip_think(stream_buf)
                incremental = new_clean[len(prev_clean):]
                if incremental:
                    if reasoning_open:
                        await hook.emit_reasoning_end()
                        reasoning_open = False
                    iter_ctx.stream_content += incremental
                    await hook.on_stream(iter_ctx, incremental)
            else:
                iter_ctx.stream_content += text
                await hook.on_stream(iter_ctx, text)

        # step64：使用 _build_request_kwargs 构建请求参数
        request_kwargs = self._build_request_kwargs(
            spec, messages, tools=tools_defs,
        )
        # on_retry_wait 需要单独检查 provider 方法签名
        retry_kwargs: dict[str, Any] = {}
        if (
            spec.retry_wait_callback is not None
            and self._provider_method_accepts(
                spec.provider, "chat_stream_with_retry", "on_retry_wait"
            )
        ):
            retry_kwargs["on_retry_wait"] = spec.retry_wait_callback

        coro = spec.provider.chat_stream_with_retry(
            on_content_delta=_on_delta,
            **request_kwargs,
            **retry_kwargs,
        )
        # step37：outer_timeout 为 None 时禁用 wait_for（持续目标 turn）。
        if outer_timeout is None:
            response = await coro
        else:
            try:
                response = await asyncio.wait_for(coro, timeout=outer_timeout)
            except asyncio.TimeoutError:
                return LLMResponse(
                    content=f"Error calling LLM: timed out after {outer_timeout:g}s",
                    finish_reason="error",
                    error_kind="timeout",
                    usage={"prompt_tokens": 0, "completion_tokens": 0},
                )
        # step64：流式结束后如果 reasoning 仍开放，emit end
        if reasoning_open:
            await hook.emit_reasoning_end()
            reasoning_open = False
        # step32（A8）：usage 缺失时按文本长度估算（~4 字符/token），保证
        # usage 累积与预算簿记不因 provider 不给 usage 而断档。
        if not response.usage:
            response = LLMResponse(
                content=response.content,
                tool_calls=response.tool_calls,
                finish_reason=response.finish_reason,
                usage=self._usage_or_estimate(spec, messages, response),
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

        # step64：malformed tool_call 处理——递归重试 + 降级无工具
        dropped, all_dropped, original_finish_reason = self._drop_malformed_tool_calls(response)
        if (
            all_dropped
            and original_finish_reason in ("tool_calls", "function_call")
            and not malformed_retry
        ):
            retry_messages = self._malformed_tool_call_retry_messages(messages, response.content)
            return await self._request_model(
                spec, retry_messages, tools_defs, hook, iter_ctx, malformed_retry=True,
            )
        if (
            all_dropped
            and original_finish_reason in ("tool_calls", "function_call")
            and malformed_retry
        ):
            fallback_messages = self._malformed_tool_call_retry_messages(messages, response.content)
            return await self._request_malformed_fallback(spec, fallback_messages)

        return response

    # ---- step64：usage 估算升级（A32）----

    @staticmethod
    def _usage_dict(usage: dict[str, Any] | None) -> dict[str, int]:
        """把 provider 返回的 usage 字典转为 int 值字典（过滤非数字值）。"""
        if not usage:
            return {}
        result: dict[str, int] = {}
        for key, value in usage.items():
            try:
                result[key] = int(value or 0)
            except (TypeError, ValueError):
                continue
        return result

    @staticmethod
    def _usage_total(usage: dict[str, int]) -> int:
        """计算 usage 总 token：优先 total_tokens，否则 prompt+completion。"""
        return max(0, usage.get("total_tokens", 0) or (
            usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)
        ))

    @staticmethod
    def _merge_usage(left: dict[str, int], right: dict[str, int]) -> dict[str, int]:
        """合并两个 usage 字典（逐键相加）。"""
        merged = dict(left)
        for key, value in right.items():
            merged[key] = merged.get(key, 0) + value
        return merged

    def _estimate_response_usage(
        self, spec: AgentRunSpec, messages: list[dict[str, Any]], response: LLMResponse
    ) -> dict[str, int]:
        """step64：provider 感知的链式估算，替代简单 chars//4。

        使用 ``estimate_prompt_tokens_chain``（考虑 provider/model/tools）+
        ``estimate_message_tokens``（assistant 消息），返回含
        ``total_tokens``/``estimated_tokens`` 的 usage 字典。
        """
        try:
            tools = spec.tools.get_definitions()
        except Exception:
            tools = None
        prompt_tokens, _ = estimate_prompt_tokens_chain(
            spec.provider, spec.model, messages, tools,
        )
        completion_tokens = estimate_message_tokens({
            "role": "assistant",
            "content": response.content or "",
        })
        total_tokens = max(0, prompt_tokens) + max(0, completion_tokens)
        if total_tokens <= 0:
            return {}
        return {
            "prompt_tokens": max(0, prompt_tokens),
            "completion_tokens": max(0, completion_tokens),
            "total_tokens": total_tokens,
            "estimated_tokens": total_tokens,
        }

    def _usage_or_estimate(
        self, spec: AgentRunSpec, messages: list[dict[str, Any]], response: LLMResponse
    ) -> dict[str, int]:
        """step64：优先用真实 usage，缺失时估算；error 响应返回空 dict。"""
        usage = self._usage_dict(response.usage)
        total = self._usage_total(usage)
        if total > 0:
            usage["total_tokens"] = total
            usage.setdefault("provider_tokens", total)
            return usage
        if response.finish_reason == "error":
            return {}
        return self._estimate_response_usage(spec, messages, response)

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
    def _drop_malformed_tool_calls(
        response: LLMResponse,
    ) -> tuple[int, bool, str | None]:
        """step64：返回 (dropped_count, all_dropped, original_finish_reason)。

        直接 mutate response.tool_calls 和 finish_reason。
        畸形 tool_call（name 缺失/非字符串）会被丢弃，避免永久 wedge session。
        """
        calls = getattr(response, "tool_calls", None)
        if not calls:
            return (0, False, getattr(response, "finish_reason", None))
        valid = [tc for tc in calls
                 if hasattr(tc, 'name') and isinstance(tc.name, str) and tc.name.strip()]
        if len(valid) == len(calls):
            return (0, False, getattr(response, "finish_reason", None))
        dropped = len(calls) - len(valid)
        original_finish_reason = getattr(response, "finish_reason", None)
        response.tool_calls = valid
        if not valid:
            response.finish_reason = "stop"
        return (dropped, not valid, original_finish_reason)

    @staticmethod
    def _malformed_tool_call_retry_messages(
        messages: list[dict[str, Any]],
        assistant_text: str | None,
    ) -> list[dict[str, Any]]:
        """step64：构造 malformed tool_call 重试提示消息（保留原 assistant 文本）。"""
        retry_messages = list(messages)
        note = (
            "The previous model response attempted to call tools, but every tool call "
            "was malformed: the tool_use blocks had missing or non-string tool names. "
            "Do not answer with a promise to use tools. Either call the required tools again "
            "using valid tool names from the provided tool list and JSON object inputs, or give "
            "a final answer only if no tool is required."
        )
        if assistant_text:
            note += f"\n\nPrevious assistant text before the malformed calls:\n{assistant_text}"
        retry_messages.append({"role": "user", "content": note})
        return retry_messages

    async def _request_malformed_fallback(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
    ) -> LLMResponse:
        """step64：malformed_retry 仍失败时降级为无工具请求（直接调 provider）。"""
        return await spec.provider.chat_with_retry(
            messages=messages, tools=None,
            model=spec.model, temperature=spec.temperature,
            max_tokens=spec.max_tokens,
        )

    @staticmethod
    def _finalization_retry_messages(
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """step64：构造 finalization 重试消息（无工具，让模型基于对话生成最终答案）。"""
        retry_messages = list(messages)
        retry_messages.append({
            "role": "user",
            "content": "Please provide your response to the user based on the conversation above.",
        })
        return retry_messages

    async def _request_finalization_retry(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        hook: AgentHook,
        iter_ctx: AgentHookContext,
    ) -> LLMResponse:
        """step64：空响应重试耗尽后发一次无工具请求。"""
        retry_messages = self._finalization_retry_messages(messages)
        return await self._request_no_tools(spec, retry_messages, hook, iter_ctx)

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
        external_lookup_counts: dict[str, int],
        workspace_violation_counts: dict[str, int],
    ) -> tuple[list[Any], list[dict[str, str]], BaseException | None]:
        """step64：返回 (results, events, fatal_error) 三元组。"""
        if len(batch) > 1 and spec.concurrent_tools:
            coros = [self._run_tool(
                tc, spec, gov_config, hook, iter_ctx, tools_used,
                external_lookup_counts, workspace_violation_counts,
            ) for tc, _ in batch]
            tool_results = await asyncio.gather(*coros)
        else:
            tool_results = []
            for tc, _ in batch:
                r = await self._run_tool(
                    tc, spec, gov_config, hook, iter_ctx, tools_used,
                    external_lookup_counts, workspace_violation_counts,
                )
                tool_results.append(r)
        results = []
        events = []
        fatal_error = None
        for result, event, error in tool_results:
            results.append(result)
            events.append(event)
            if error is not None and fatal_error is None:
                fatal_error = error
        return results, events, fatal_error

    async def _run_tool(
        self,
        tc: Any,
        spec: AgentRunSpec,
        gov_config: ContextGovernanceConfig,
        hook: AgentHook,
        iter_ctx: AgentHookContext,
        tools_used: list[str],
        external_lookup_counts: dict[str, int],
        workspace_violation_counts: dict[str, int],
    ) -> tuple[Any, dict[str, str], BaseException | None]:
        """step64：返回 (result, event, error) 三元组 + hook 生命周期 + 安全检测。"""
        name = tc.name if hasattr(tc, 'name') else str(tc)
        tools_used.append(name)

        # step64：重复外部查找阻断
        lookup_error = repeated_external_lookup_error(name, tc.arguments, external_lookup_counts)
        if lookup_error:
            event = {"name": name, "status": "error", "detail": "repeated external lookup blocked"}
            if spec.fail_on_tool_error:
                return lookup_error, event, RuntimeError(lookup_error)
            return lookup_error, event, None

        tool, params = None, tc.arguments
        if hasattr(spec.tools, 'prepare_call'):
            tool, params, error = spec.tools.prepare_call(name, tc.arguments)
            if error:
                event = {"name": name, "status": "error", "detail": str(error)[:120]}
                # step64：prepare_call 出错时检测安全边界
                handled = self._classify_violation(
                    raw_text=str(error),
                    soft_payload=str(error),
                    event=event,
                    tool_call=tc,
                    workspace_violation_counts=workspace_violation_counts,
                )
                if handled is not None:
                    return handled
                if spec.fail_on_tool_error:
                    return str(error), event, RuntimeError(str(error))
                return str(error), event, None
        await hook.before_execute_tool(iter_ctx, tc, tool, params)
        try:
            if tool is not None:
                result = await tool.execute(**params)
            else:
                result = await spec.tools.execute(name, **tc.arguments)
        except asyncio.CancelledError:
            raise  # step64：不捕获，向上传播
        except BaseException as exc:
            await hook.on_execute_tool_error(iter_ctx, tc, tool, params, exc)
            event = {"name": name, "status": "error", "detail": str(exc)[:120]}
            payload = f"Error: {type(exc).__name__}: {exc}"
            # step64：工具执行异常时检测安全边界
            handled = self._classify_violation(
                raw_text=str(exc),
                soft_payload=payload,
                event=event,
                tool_call=tc,
                workspace_violation_counts=workspace_violation_counts,
            )
            if handled is not None:
                return handled
            if spec.fail_on_tool_error:
                return payload, event, exc
            return payload, event, None
        # step64：工具返回错误结果时检测安全边界
        if isinstance(result, ToolResult) and result.is_error:
            await hook.on_execute_tool_error(iter_ctx, tc, tool, params, result)
            event = {"name": name, "status": "error", "detail": str(result)[:120]}
            handled = self._classify_violation(
                raw_text=str(result),
                soft_payload=str(result),
                event=event,
                tool_call=tc,
                workspace_violation_counts=workspace_violation_counts,
            )
            if handled is not None:
                return handled
            if spec.fail_on_tool_error:
                return str(result), event, RuntimeError(str(result))
            return str(result), event, None
        await self._emit_tool_progress(spec, name, result)
        normalized = _GOVERNOR.normalize_tool_result(
            gov_config, tc.id if hasattr(tc, 'id') else "", name, result,
        )
        await hook.after_execute_tool(iter_ctx, tc, tool, params, normalized)
        detail = str(normalized).replace("\n", " ").strip()[:120] or "(empty)"
        event = {"name": name, "status": "ok", "detail": detail}
        return normalized, event, None

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
        # step64：支持 callable goal_continue_message（闭包动态读取 session.metadata）
        msg = spec.goal_continue_message
        if callable(msg):
            msg = msg()
        return {
            "role": "user",
            "content": msg or (
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
        from step65.session.history_visibility import is_hidden_history_message

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
        # step64：SSRF/workspace 安全检测计数（整个 turn 中持久化）
        external_lookup_counts: dict[str, int] = {}
        workspace_violation_counts: dict[str, int] = {}
        # step64：工具执行事件追踪
        tool_events: list[dict[str, str]] = []

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
                    tool_events=tool_events,
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
                # step64：malformed 已在 _request_model 中处理，直接用 response
                assistant_msg = self._build_assistant_message(response)
                messages.append(assistant_msg)
                iter_ctx.tool_calls = list(response.tool_calls)

                await self._emit_checkpoint(
                    spec,
                    {
                        "phase": "awaiting_tools",
                        "iteration": iteration,
                        "model": spec.model,
                        "assistant_message": assistant_msg,
                        "completed_tool_results": [],
                        "pending_tool_calls": self._openai_tool_calls(response.tool_calls),
                    },
                )

                batches = self._partition_tool_batches(spec, response.tool_calls)
                completed_tool_results: list[dict[str, Any]] = []
                fatal_error = None
                for batch in batches:
                    results, events, batch_fatal_error = await self._execute_tool_batch(
                        batch, spec, gov_config, hook, iter_ctx, tools_used,
                        external_lookup_counts, workspace_violation_counts,
                    )
                    for used_tc, result in zip([tc for tc, _ in batch], results):
                        tool_msg = {
                            "role": "tool", "tool_call_id": used_tc.id,
                            "name": used_tc.name, "content": str(result),
                        }
                        messages.append(tool_msg)
                        completed_tool_results.append(tool_msg)
                        iter_ctx.tool_results.append(str(result))
                    # step64：收集 tool_events
                    tool_events.extend(events)
                    if batch_fatal_error is not None and fatal_error is None:
                        fatal_error = batch_fatal_error
                        break
                # step64：fatal_error 非 None 时终止 turn
                if fatal_error is not None:
                    error_msg = f"Error: {type(fatal_error).__name__}: {fatal_error}"
                    messages.append({"role": "assistant", "content": error_msg})
                    iter_ctx.final_content = error_msg
                    iter_ctx.error = error_msg
                    iter_ctx.stop_reason = "tool_error"
                    await hook.after_iteration(iter_ctx)
                    return AgentRunResult(
                        final_content=error_msg,
                        messages=messages,
                        tools_used=tools_used,
                        usage=total_usage,
                        stop_reason="tool_error",
                        goal_continuation_rounds=spec.goal_continuation_rounds,
                        error=error_msg,
                        had_injections=had_injections,
                        tool_events=tool_events,
                    )

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

            # step64：提取 reasoning，分离 reasoning_content/thinking_blocks 和 content
            reasoning_text, cleaned_content = extract_reasoning(
                response.reasoning_content, response.thinking_blocks, response.content,
            )
            response.content = cleaned_content

            # step64：输出 reasoning 流（非流式，一次性输出；流式留到 step64）
            if reasoning_text and not iter_ctx.streamed_reasoning:
                await hook.emit_reasoning(reasoning_text)
                await hook.emit_reasoning_end()
                iter_ctx.streamed_reasoning = True

            # step64：用 hook.finalize_content 替代直接用 response.content
            clean = hook.finalize_content(iter_ctx, response.content) or ""

            if self._is_blank_text(clean):
                if empty_retries < _MAX_EMPTY_RETRIES:
                    empty_retries += 1
                    # 空响应重试：先关流，避免上一段空流悬挂。
                    await hook.on_stream_end(iter_ctx, resuming=False)
                    continue

                # step64：空响应重试耗尽后发 finalization retry（无工具请求），
                # 失败或仍为空时 fallback 到 _EMPTY_FINAL_RESPONSE_MESSAGE。
                await hook.on_stream_end(iter_ctx, resuming=False)
                try:
                    final_response = await self._request_finalization_retry(
                        spec, messages, hook, iter_ctx,
                    )
                    final_content = final_response.content or _EMPTY_FINAL_RESPONSE_MESSAGE
                except Exception:
                    final_content = _EMPTY_FINAL_RESPONSE_MESSAGE

                # step64：使用 _append_final_message 避免重复
                self._append_final_message(messages, final_content)
                iter_ctx.final_content = final_content
                iter_ctx.stop_reason = "empty_final_response"
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
                    tool_events=tool_events,
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
                tool_events=tool_events,
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
            # step64：使用 _append_final_message 避免重复
            self._append_final_message(messages, final_content)
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
            tool_events=tool_events,
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
        # step64：用 hook.finalize_content 替代直接用 response.content
        clean = hook.finalize_content(iter_ctx, response.content) or ""
        if not clean.strip():
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
        """step64：累计 response.usage 中的所有键（包括 total_tokens/estimated_tokens）。"""
        if response.usage:
            for key, value in response.usage.items():
                if value:
                    total[key] = total.get(key, 0) + value

    # ---- step64：SSRF/workspace 安全检测 ----

    _SSRF_MARKERS: tuple[str, ...] = (
        "internal/private url detected",
        "private/internal address",
        "private address",
    )
    _SSRF_BOUNDARY_NOTE: str = (
        "This is a non-bypassable security boundary. Stop trying to access "
        "private/internal URLs. Do not retry with curl, wget, encoded IPs, "
        "alternate DNS, redirects, proxies, or another tool. Ask the user for "
        "local files, logs, screenshots, or an explicit safe public URL instead. "
        "If the user explicitly trusts this private URL, ask them to whitelist "
        "the exact IP/CIDR via tools.ssrfWhitelist."
    )
    _WORKSPACE_VIOLATION_MARKERS: tuple[str, ...] = (
        "outside the configured workspace",
        "outside allowed directory",
        "working_dir is outside",
        "working_dir could not be resolved",
        "path outside working dir",
        "path traversal detected",
    )

    @classmethod
    def _is_ssrf_violation(cls, text: str) -> bool:
        if not text:
            return False
        lowered = text.lower()
        return any(marker in lowered for marker in cls._SSRF_MARKERS)

    @classmethod
    def _is_workspace_violation(cls, text: str) -> bool:
        """True when *text* looks like any policy boundary rejection."""
        if not text:
            return False
        lowered = text.lower()
        if cls._is_ssrf_violation(lowered):
            return True
        return any(marker in lowered for marker in cls._WORKSPACE_VIOLATION_MARKERS)

    @classmethod
    def _ssrf_soft_payload(cls, raw_text: str) -> str:
        text = raw_text.strip() or "Error: request blocked by SSRF guard"
        return f"{text}\n\n{cls._SSRF_BOUNDARY_NOTE}"

    @staticmethod
    def _event_detail(prefix: str, text: str, limit: int = 160) -> str:
        return (prefix + text.replace("\n", " ").strip())[:limit]

    def _classify_violation(
        self,
        *,
        raw_text: str,
        soft_payload: str,
        event: dict[str, str],
        tool_call: Any,
        workspace_violation_counts: dict[str, int],
    ) -> tuple[Any, dict[str, str], BaseException | None] | None:
        """统一分类安全边界失败；返回 None 表示非安全违规，继续正常处理。"""
        if self._is_ssrf_violation(raw_text):
            event["detail"] = self._event_detail("ssrf_violation: ", raw_text)
            return self._ssrf_soft_payload(raw_text), event, None
        if self._is_workspace_violation(raw_text):
            escalation = repeated_workspace_violation_error(
                tool_call.name if hasattr(tool_call, "name") else str(tool_call),
                tool_call.arguments if hasattr(tool_call, "arguments") else {},
                workspace_violation_counts,
            )
            event["detail"] = self._event_detail("workspace_violation: ", raw_text)
            if escalation is not None:
                event["detail"] = self._event_detail(
                    "workspace_violation_escalated: ", raw_text,
                )
                return escalation, event, None
            return soft_payload, event, None
        return None
