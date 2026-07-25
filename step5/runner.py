from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from step5.llm import LLMResponse
from step5.provider import LLMProvider
from step5.tool import ToolRegistry


@dataclass
class AgentRunSpec:
    initial_messages: list[dict[str, Any]]
    tools: ToolRegistry
    provider: LLMProvider
    max_iterations: int = 10
    model: str | None = None
    temperature: float = 0.7
    max_tokens: int = 4096


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

        for _ in range(spec.max_iterations):
            tools_defs = spec.tools.get_definitions() or None
            response = await spec.provider.chat_with_retry(
                messages=messages,
                tools=tools_defs,
                model=spec.model,
                temperature=spec.temperature,
                max_tokens=spec.max_tokens,
            )
            self._accumulate_usage(total_usage, response)

            if response.tool_calls and response.finish_reason == "tool_calls":
                assistant_msg = self._build_assistant_message(response)
                messages.append(assistant_msg)

                for tc in response.tool_calls:
                    result = await spec.tools.execute(tc.name, **tc.arguments)
                    tools_used.append(tc.name)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "name": tc.name,
                        "content": str(result),
                    })
                continue

            messages.append({"role": "assistant", "content": response.content})
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
