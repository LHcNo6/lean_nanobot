"""LLM provider abstraction with streaming support."""

from __future__ import annotations

import asyncio
import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv()

_STREAM_IDLE_TIMEOUT_S = 30.0


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class ToolCallRequest:
    id: str
    name: str
    arguments: Any


@dataclass
class LLMResponse:
    content: str | None
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class LLMProvider(ABC):
    """Abstract LLM provider."""

    @abstractmethod
    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        """Send a chat completion request (non-streaming)."""
        ...

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        on_content_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        """Stream a chat completion.

        Default fallback: call ``chat()`` and deliver full content as single delta.
        Providers with native SSE support should override this method.
        """
        response = await self.chat(
            messages=messages, tools=tools, model=model,
            temperature=temperature, max_tokens=max_tokens,
        )
        if on_content_delta and response.content:
            await on_content_delta(response.content)
        return response


# ---------------------------------------------------------------------------
# OpenAI-compatible implementation
# ---------------------------------------------------------------------------

class OpenAICompatProvider(LLMProvider):
    """Provider backed by the OpenAI Python SDK (works with any OpenAI-compatible API)."""

    def __init__(
        self,
        api_key: str,
        api_base: str = "https://api.openai.com/v1",
        model: str = "gpt-4o-mini",
    ) -> None:
        self._client = AsyncOpenAI(api_key=api_key, base_url=api_base)
        self._default_model = model

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
        except asyncio.TimeoutError:
            return LLMResponse(
                content=None,
                finish_reason="error",
                usage={"prompt_tokens": 0, "completion_tokens": 0},
            )
        except StopAsyncIteration:
            pass

        return self._assemble_from_chunks(chunks)

    # ---- internal helpers -------------------------------------------------

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
                # Accumulate tool call deltas
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
                args = json.loads(args) if args.strip() else {}
            tool_calls.append(ToolCallRequest(
                id=raw["id"],
                name=raw["name"],
                arguments=args,
            ))

        # Usage comes in the final chunk
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

    @classmethod
    def from_env(cls) -> OpenAICompatProvider:
        """Create a provider from ``.env`` / environment variables."""
        return cls(
            api_key=os.environ["OPENAI_API_KEY"],
            api_base=os.environ.get("OPENAI_API_BASE", "https://api.openai.com/v1"),
            model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
        )
