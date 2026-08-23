from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable

from openai import AsyncOpenAI

from step86.llm import LLMResponse, ToolCallRequest
from step86.provider import LLMProvider

_STREAM_IDLE_TIMEOUT_S = 30.0


class OpenAICompatProvider(LLMProvider):
    def __init__(
        self,
        api_key: str | None = None,
        api_base: str = "https://api.openai.com/v1",
        model: str = "gpt-4o-mini",
    ) -> None:
        # 本地/直连 provider 可无 key；openai SDK 要求非空，用占位符兜底
        # （factory 已保证非 exempt provider 必须配置 key）。
        self._client = AsyncOpenAI(
            api_key=api_key or "missing",
            base_url=api_base,
            max_retries=0,
        )
        self._default_model = model

    @property
    def model(self) -> str:
        return self._default_model

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        kwargs = self._build_kwargs(messages, tools, model, temperature, max_tokens)
        resp = await self._client.chat.completions.create(**kwargs)
        return self._parse_response(resp)

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        on_content_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        kwargs = self._build_kwargs(messages, tools, model, temperature, max_tokens)
        kwargs["stream"] = True
        kwargs["stream_options"] = {"include_usage": True}

        stream = await self._client.chat.completions.create(**kwargs)
        chunks: list[Any] = []
        stream_iter = stream.__aiter__()

        try:
            while True:
                chunk = await asyncio.wait_for(
                    stream_iter.__anext__(),
                    timeout=_STREAM_IDLE_TIMEOUT_S,
                )
                chunks.append(chunk)
                if chunk.choices and on_content_delta:
                    delta = chunk.choices[0].delta
                    if delta.content:
                        await on_content_delta(delta.content)
        except StopAsyncIteration:
            pass

        return self._assemble_from_chunks(chunks)

    def _build_kwargs(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = dict(
            model=model or self._default_model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        if tools:
            kwargs["tools"] = tools
        return kwargs

    def _parse_response(self, resp: Any) -> LLMResponse:
        choice = resp.choices[0]
        msg = choice.message
        tool_calls = self._parse_tool_calls(msg.tool_calls)
        finish_reason = choice.finish_reason or "stop"
        usage_raw = resp.usage
        usage = {
            "prompt_tokens": usage_raw.prompt_tokens if usage_raw else 0,
            "completion_tokens": usage_raw.completion_tokens if usage_raw else 0,
        }
        return LLMResponse(
            content=msg.content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=usage,
        )

    def _assemble_from_chunks(self, chunks: list[Any]) -> LLMResponse:
        if not chunks:
            return LLMResponse(content="", finish_reason="stop")

        full_content = ""
        finish_reason = "stop"
        tool_calls_raw: dict[int, dict] = {}

        for chunk in chunks:
            if chunk.choices:
                choice = chunk.choices[0]
                delta = choice.delta
                if delta.content:
                    full_content += delta.content
                if choice.finish_reason:
                    finish_reason = choice.finish_reason
                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index
                        if idx not in tool_calls_raw:
                            tool_calls_raw[idx] = {"id": "", "name": "", "arguments": ""}
                        if tc.id:
                            tool_calls_raw[idx]["id"] = tc.id
                        if tc.function:
                            if tc.function.name:
                                tool_calls_raw[idx]["name"] = tc.function.name
                            if tc.function.arguments:
                                tool_calls_raw[idx]["arguments"] += tc.function.arguments

        tool_calls: list[ToolCallRequest] = []
        for idx in sorted(tool_calls_raw):
            raw = tool_calls_raw[idx]
            args = raw["arguments"]
            if isinstance(args, str) and args.strip():
                args = json.loads(args)
            tool_calls.append(ToolCallRequest(
                id=raw["id"],
                name=raw["name"],
                arguments=args,
            ))

        usage: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0}
        last = chunks[-1]
        if last.usage:
            usage = {
                "prompt_tokens": last.usage.prompt_tokens or 0,
                "completion_tokens": last.usage.completion_tokens or 0,
            }

        return LLMResponse(
            content=full_content or None,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=usage,
        )

    @staticmethod
    def _parse_tool_calls(tool_calls_raw: Any) -> list[ToolCallRequest]:
        if not tool_calls_raw:
            return []
        result: list[ToolCallRequest] = []
        for tc in tool_calls_raw:
            args = tc.function.arguments
            if isinstance(args, str) and args.strip():
                args = json.loads(args)
            result.append(ToolCallRequest(
                id=tc.id,
                name=tc.function.name,
                arguments=args,
            ))
        return result
