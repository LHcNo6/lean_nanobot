"""step87：MCP Prompt 支持单元测试。"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from step104.tools.mcp import (
    MCPClient,
    MCPPromptWrapper,
    create_mcp_prompts,
)


def _run(coro):
    return asyncio.run(coro)


def _make_mock_client(initialized: bool = True) -> MCPClient:
    client = MCPClient("test", server_name="test")
    if initialized:
        client._initialized = True
        client._process = MagicMock()
    return client


class TestMCPClientPrompts:
    """MCPClient prompt 方法。"""

    def test_list_prompts_not_initialized(self) -> None:
        client = _make_mock_client(initialized=False)
        with pytest.raises(RuntimeError, match="not initialized"):
            _run(client.list_prompts())

    def test_get_prompt_not_initialized(self) -> None:
        client = _make_mock_client(initialized=False)
        with pytest.raises(RuntimeError, match="not initialized"):
            _run(client.get_prompt("test"))

    @pytest.mark.asyncio
    async def test_list_prompts_request(self) -> None:
        client = _make_mock_client()
        client._send_request = AsyncMock(return_value={
            "jsonrpc": "2.0", "id": 1,
            "result": {"prompts": [
                {"name": "summarize", "description": "Summarize text", "arguments": [
                    {"name": "text", "description": "Text to summarize", "required": True},
                ]},
                {"name": "translate", "description": "Translate text", "arguments": []},
            ]},
        })

        prompts = await client.list_prompts()

        assert len(prompts) == 2
        assert prompts[0]["name"] == "summarize"
        call_args = client._send_request.call_args[0][0]
        assert call_args["method"] == "prompts/list"

    @pytest.mark.asyncio
    async def test_get_prompt_request(self) -> None:
        client = _make_mock_client()
        client._send_request = AsyncMock(return_value={
            "jsonrpc": "2.0", "id": 1,
            "result": {
                "description": "Summarized",
                "messages": [{"role": "user", "content": {"type": "text", "text": "Summary: ..."}}],
            },
        })

        result = await client.get_prompt("summarize", {"text": "hello"})

        assert result["result"]["messages"][0]["content"]["text"] == "Summary: ..."
        call_args = client._send_request.call_args[0][0]
        assert call_args["method"] == "prompts/get"
        assert call_args["params"]["name"] == "summarize"
        assert call_args["params"]["arguments"] == {"text": "hello"}

    @pytest.mark.asyncio
    async def test_get_prompt_no_arguments(self) -> None:
        client = _make_mock_client()
        client._send_request = AsyncMock(return_value={"result": {"messages": []}})

        await client.get_prompt("simple")

        call_args = client._send_request.call_args[0][0]
        assert "arguments" not in call_args["params"]


class TestMCPPromptWrapper:
    """MCPPromptWrapper。"""

    def test_name_format(self) -> None:
        client = _make_mock_client()
        info = {"name": "summarize", "description": "Summarize", "arguments": []}
        wrapper = MCPPromptWrapper(client, info, server_name="myserver")

        assert wrapper.name == "mcp_myserver_prompt_summarize"

    def test_name_sanitize(self) -> None:
        client = _make_mock_client()
        info = {"name": "my prompt!", "description": "Test", "arguments": []}
        wrapper = MCPPromptWrapper(client, info, server_name="srv")

        assert wrapper.name == "mcp_srv_prompt_my_prompt"

    def test_description(self) -> None:
        client = _make_mock_client()
        wrapper = MCPPromptWrapper(client, {"name": "x", "description": "A prompt"})
        assert wrapper.description == "A prompt"

    def test_default_description(self) -> None:
        client = _make_mock_client()
        wrapper = MCPPromptWrapper(client, {"name": "x"})
        assert "MCP prompt" in wrapper.description

    def test_read_only(self) -> None:
        client = _make_mock_client()
        wrapper = MCPPromptWrapper(client, {"name": "x"})
        assert wrapper.read_only is True

    def test_not_auto_discoverable(self) -> None:
        assert MCPPromptWrapper._plugin_discoverable is False

    def test_dynamic_parameters(self) -> None:
        client = _make_mock_client()
        info = {"name": "summarize", "description": "Sum", "arguments": [
            {"name": "text", "description": "Text to summarize", "required": True},
            {"name": "style", "description": "Output style", "required": False},
        ]}
        wrapper = MCPPromptWrapper(client, info)
        params = wrapper.parameters

        assert params["type"] == "object"
        assert "text" in params["properties"]
        assert "style" in params["properties"]
        assert params["required"] == ["text"]
        assert params["properties"]["text"]["type"] == "string"

    def test_no_arguments_parameters(self) -> None:
        client = _make_mock_client()
        wrapper = MCPPromptWrapper(client, {"name": "simple", "arguments": []})
        params = wrapper.parameters
        assert params["properties"] == {}
        assert params["required"] == []

    def test_prompt_name_property(self) -> None:
        client = _make_mock_client()
        wrapper = MCPPromptWrapper(client, {"name": "my_prompt"})
        assert wrapper.prompt_name == "my_prompt"

    def test_to_schema(self) -> None:
        client = _make_mock_client()
        wrapper = MCPPromptWrapper(client, {"name": "p", "description": "D"}, server_name="s")
        schema = wrapper.to_schema()
        assert schema["function"]["name"] == "mcp_s_prompt_p"

    @pytest.mark.asyncio
    async def test_execute_success(self) -> None:
        client = _make_mock_client()
        client.get_prompt = AsyncMock(return_value={
            "jsonrpc": "2.0", "id": 1,
            "result": {"messages": [
                {"role": "user", "content": {"type": "text", "text": "Please summarize: hello"}},
            ]},
        })
        wrapper = MCPPromptWrapper(client, {"name": "summarize", "arguments": []})

        result = await wrapper.execute(text="hello")
        assert "Please summarize: hello" in str(result)
        assert "[user]" in str(result)

    @pytest.mark.asyncio
    async def test_execute_string_content(self) -> None:
        client = _make_mock_client()
        client.get_prompt = AsyncMock(return_value={
            "result": {"messages": [{"role": "assistant", "content": "Direct string content"}]},
        })
        wrapper = MCPPromptWrapper(client, {"name": "x"})

        result = await wrapper.execute()
        assert "Direct string content" in str(result)
        assert "[assistant]" in str(result)

    @pytest.mark.asyncio
    async def test_execute_jsonrpc_error(self) -> None:
        from step104.tool import ToolResult
        client = _make_mock_client()
        client.get_prompt = AsyncMock(return_value={
            "error": {"code": -32601, "message": "Prompt not found"},
        })
        wrapper = MCPPromptWrapper(client, {"name": "x"})

        result = await wrapper.execute()
        assert isinstance(result, ToolResult)
        assert result.is_error
        assert "Prompt not found" in str(result)


class TestCreateMcpPrompts:
    """create_mcp_prompts 辅助函数。"""

    @patch("asyncio.create_subprocess_exec")
    def test_create_prompts(self, mock_exec) -> None:
        mock_process = MagicMock()
        mock_process.stdin = MagicMock()
        mock_process.stdin.write = MagicMock()
        mock_process.stdin.drain = AsyncMock()
        mock_process.stdout = MagicMock()
        responses = [
            json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}).encode(),
            json.dumps({
                "jsonrpc": "2.0", "id": 2,
                "result": {"prompts": [
                    {"name": "prompt_a", "description": "A", "arguments": []},
                    {"name": "prompt_b", "description": "B", "arguments": []},
                ]},
            }).encode(),
        ]
        mock_process.stdout.readline = AsyncMock(side_effect=responses)
        mock_process.terminate = MagicMock()
        mock_process.wait = AsyncMock(return_value=0)
        mock_exec.return_value = mock_process

        client = MCPClient("test", server_name="myserver")
        wrappers = _run(create_mcp_prompts(client))

        assert len(wrappers) == 2
        assert wrappers[0].name == "mcp_myserver_prompt_prompt_a"
        assert wrappers[1].prompt_name == "prompt_b"
