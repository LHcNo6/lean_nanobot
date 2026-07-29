from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from step13.hook import AgentHook, AgentHookContext, AgentRunHookContext
from step13.llm import LLMResponse
from step13.provider import LLMProvider
from step13.tool import ToolRegistry


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


@dataclass
class AgentRunResult:
    final_content: str | None
    messages: list[dict[str, Any]]
    tools_used: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    stop_reason: str = "completed"

    @property
    def total_prompt_tokens(self) -> int:
        return self.usage.get("prompt_tokens", 0)

    @property
    def total_completion_tokens(self) -> int:
        return self.usage.get("completion_tokens", 0)


_MAX_ITERATIONS_FALLBACK = "Reached max iterations without a final response."


class AgentRunner:
    async def run(self, spec: AgentRunSpec) -> AgentRunResult:
        messages = list(spec.initial_messages)
        tools_used: list[str] = []
        total_usage: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0}
        hook = spec.hook or AgentHook()

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
            run_ctx.final_content = result.final_content
            run_ctx.tools_used = list(result.tools_used)
            run_ctx.usage = dict(result.usage)
            run_ctx.stop_reason = result.stop_reason
            await hook.after_run(run_ctx)
            return result
        finally:
            await hook.on_finally(run_ctx)

    async def _run_loop(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        tools_used: list[str],
        total_usage: dict[str, int],
        hook: AgentHook,
    ) -> AgentRunResult:
        for iteration in range(spec.max_iterations):
            iter_ctx = AgentHookContext(
                iteration=iteration,
                messages=messages,
                session_key=spec.session_key,
            )
            await hook.before_iteration(iter_ctx)

            tools_defs = spec.tools.get_definitions() or None

            async def on_delta(text: str) -> None:
                iter_ctx.stream_content += text
                await hook.on_stream(iter_ctx, text)

            response = await spec.provider.chat_stream_with_retry(
                messages=messages,
                tools=tools_defs,
                model=spec.model,
                temperature=spec.temperature,
                max_tokens=spec.max_tokens,
                on_content_delta=on_delta,
            )
            self._accumulate_usage(total_usage, response)
            iter_ctx.response = response
            iter_ctx.usage = dict(total_usage)
            await hook.on_stream_end(iter_ctx)

            if response.tool_calls and response.finish_reason == "tool_calls":
                assistant_msg = self._build_assistant_message(response)
                messages.append(assistant_msg)
                iter_ctx.tool_calls = list(response.tool_calls)

                for tc in response.tool_calls:
                    result = await spec.tools.execute(tc.name, **tc.arguments)
                    tools_used.append(tc.name)
                    tool_msg = {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "name": tc.name,
                        "content": str(result),
                    }
                    messages.append(tool_msg)
                    iter_ctx.tool_results.append(str(result))

                iter_ctx.stop_reason = "tool_calls"
                await hook.after_iteration(iter_ctx)
                if spec.injection_callback:
                    injected = await spec.injection_callback()
                    for msg in injected:
                        messages.append(msg)
                continue

            messages.append({"role": "assistant", "content": response.content})
            iter_ctx.final_content = response.content
            iter_ctx.stop_reason = response.finish_reason
            await hook.after_iteration(iter_ctx)

            if spec.injection_callback:
                injected = await spec.injection_callback()
                if injected:
                    for msg in injected:
                        messages.append(msg)
                    continue

            return AgentRunResult(
                final_content=response.content,
                messages=messages,
                tools_used=tools_used,
                usage=total_usage,
                stop_reason=response.finish_reason,
            )

        return AgentRunResult(
            final_content=_MAX_ITERATIONS_FALLBACK,
            messages=messages,
            tools_used=tools_used,
            usage=total_usage,
            stop_reason="max_iterations",
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
