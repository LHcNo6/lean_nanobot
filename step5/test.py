"""Tests for Step 5 — AgentRunner tool-calling loop."""

import json
import unittest
from typing import Any

from step5.llm import LLMResponse, ToolCallRequest
from step5.runner import AgentRunSpec, AgentRunResult, AgentRunner
from step5.tool import ToolRegistry, ToolResult
from step5.tools.echo import EchoTool


class _MockProvider:
    """Provider that returns pre-defined LLMResponse objects in sequence."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = list(responses)
        self.call_count = 0
        self.calls: list[dict] = []

    async def chat_with_retry(self, **kwargs: Any) -> LLMResponse:
        self.call_count += 1
        self.calls.append(kwargs)
        idx = self.call_count - 1
        if idx < len(self.responses):
            return self.responses[idx]
        return LLMResponse(content="fallback", finish_reason="stop")


class TestAgentRunner(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.registry = ToolRegistry()
        self.registry.register(EchoTool())

    def _run(self, provider, **overrides):
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "Hi"}],
            tools=self.registry,
            provider=provider,
            max_iterations=5,
            **overrides,
        )
        return AgentRunner().run(spec)

    async def test_direct_text_response(self):
        """LLM returns text directly — no tool calls."""
        provider = _MockProvider([
            LLMResponse(content="Hello world", finish_reason="stop", usage={"prompt_tokens": 5, "completion_tokens": 3}),
        ])
        result = await self._run(provider)
        self.assertEqual(result.final_content, "Hello world")
        self.assertEqual(result.stop_reason, "stop")
        self.assertEqual(result.tools_used, [])
        self.assertEqual(provider.call_count, 1)

    async def test_tool_call_then_text(self):
        """LLM returns tool_calls first, then final text."""
        tc = ToolCallRequest(id="call_1", name="echo", arguments={"text": "hello"})
        provider = _MockProvider([
            LLMResponse(content=None, tool_calls=[tc], finish_reason="tool_calls", usage={"prompt_tokens": 5, "completion_tokens": 2}),
            LLMResponse(content="Done", finish_reason="stop", usage={"prompt_tokens": 10, "completion_tokens": 5}),
        ])
        result = await self._run(provider)
        self.assertEqual(result.final_content, "Done")
        self.assertEqual(result.tools_used, ["echo"])
        self.assertEqual(result.stop_reason, "stop")
        self.assertEqual(provider.call_count, 2)

    async def test_tool_result_in_messages(self):
        """Verify the tool result message is correctly appended."""
        tc = ToolCallRequest(id="call_1", name="echo", arguments={"text": "hello"})
        provider = _MockProvider([
            LLMResponse(content=None, tool_calls=[tc], finish_reason="tool_calls"),
            LLMResponse(content="Done", finish_reason="stop"),
        ])
        result = await self._run(provider)
        # Find the tool result message
        tool_msgs = [m for m in result.messages if m.get("role") == "tool"]
        self.assertEqual(len(tool_msgs), 1)
        self.assertEqual(tool_msgs[0]["tool_call_id"], "call_1")
        self.assertEqual(tool_msgs[0]["name"], "echo")
        self.assertEqual(tool_msgs[0]["content"], "Echo: hello")

    async def test_max_iterations(self):
        """LLM keeps requesting tools until iteration limit."""
        tc = ToolCallRequest(id="call_1", name="echo", arguments={"text": "x"})
        provider = _MockProvider([
            LLMResponse(content=None, tool_calls=[tc], finish_reason="tool_calls"),
            LLMResponse(content=None, tool_calls=[tc], finish_reason="tool_calls"),
            LLMResponse(content=None, tool_calls=[tc], finish_reason="tool_calls"),
        ])
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "Hi"}],
            tools=self.registry,
            provider=provider,
            max_iterations=2,
        )
        result = await AgentRunner().run(spec)
        self.assertEqual(result.stop_reason, "max_iterations")
        self.assertIn("max iterations", result.final_content.lower())
        self.assertEqual(provider.call_count, 2)

    async def test_multiple_tool_calls_in_one_turn(self):
        """LLM returns two tool_calls in a single response."""
        tc1 = ToolCallRequest(id="call_1", name="echo", arguments={"text": "one"})
        tc2 = ToolCallRequest(id="call_2", name="echo", arguments={"text": "two"})
        provider = _MockProvider([
            LLMResponse(content=None, tool_calls=[tc1, tc2], finish_reason="tool_calls"),
            LLMResponse(content="All done", finish_reason="stop"),
        ])
        result = await self._run(provider)
        self.assertEqual(result.tools_used, ["echo", "echo"])
        tool_msgs = [m for m in result.messages if m.get("role") == "tool"]
        self.assertEqual(len(tool_msgs), 2)
        self.assertEqual(tool_msgs[0]["content"], "Echo: one")
        self.assertEqual(tool_msgs[1]["content"], "Echo: two")

    async def test_tool_execution_error_propagates(self):
        """Tool returning is_error=True still continues the loop."""
        class _ErrorTool(EchoTool):
            async def execute(self, **kwargs):
                return ToolResult("something broke", is_error=True)

        registry = ToolRegistry()
        registry.register(_ErrorTool())

        tc = ToolCallRequest(id="call_1", name="echo", arguments={"text": "x"})
        provider = _MockProvider([
            LLMResponse(content=None, tool_calls=[tc], finish_reason="tool_calls"),
            LLMResponse(content="Recovered", finish_reason="stop"),
        ])
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "Hi"}],
            tools=registry,
            provider=provider,
            max_iterations=5,
        )
        result = await AgentRunner().run(spec)
        self.assertEqual(result.final_content, "Recovered")
        tool_msgs = [m for m in result.messages if m.get("role") == "tool"]
        self.assertEqual(len(tool_msgs), 1)
        self.assertIn("broke", tool_msgs[0]["content"])

    async def test_usage_accumulated(self):
        """Usage from multiple iterations is summed."""
        tc = ToolCallRequest(id="call_1", name="echo", arguments={"text": "x"})
        provider = _MockProvider([
            LLMResponse(content=None, tool_calls=[tc], finish_reason="tool_calls",
                        usage={"prompt_tokens": 5, "completion_tokens": 2}),
            LLMResponse(content="Done", finish_reason="stop",
                        usage={"prompt_tokens": 10, "completion_tokens": 5}),
        ])
        result = await self._run(provider)
        self.assertEqual(result.total_prompt_tokens, 15)
        self.assertEqual(result.total_completion_tokens, 7)

    async def test_empty_tools_no_tool_calls(self):
        """No tools registered — LLM just returns text."""
        empty_registry = ToolRegistry()
        provider = _MockProvider([
            LLMResponse(content="No tools here", finish_reason="stop"),
        ])
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "Hi"}],
            tools=empty_registry,
            provider=provider,
        )
        result = await AgentRunner().run(spec)
        self.assertEqual(result.final_content, "No tools here")
        self.assertEqual(result.tools_used, [])

    async def test_assistant_message_tool_calls_format(self):
        """Verify the assistant message has correct OpenAI tool_calls format."""
        tc = ToolCallRequest(id="call_1", name="echo", arguments={"text": "hi"})
        provider = _MockProvider([
            LLMResponse(content=None, tool_calls=[tc], finish_reason="tool_calls"),
            LLMResponse(content="Done", finish_reason="stop"),
        ])
        result = await self._run(provider)
        # Find the assistant message with tool_calls
        assistant_msgs = [m for m in result.messages if m.get("tool_calls")]
        self.assertEqual(len(assistant_msgs), 1)
        asm = assistant_msgs[0]
        self.assertEqual(asm["role"], "assistant")
        self.assertEqual(len(asm["tool_calls"]), 1)
        tcf = asm["tool_calls"][0]
        self.assertEqual(tcf["id"], "call_1")
        self.assertEqual(tcf["type"], "function")
        self.assertEqual(tcf["function"]["name"], "echo")
        self.assertEqual(json.loads(tcf["function"]["arguments"]), {"text": "hi"})





if __name__ == "__main__":
    unittest.main()
