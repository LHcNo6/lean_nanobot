"""step64 测试：TurnContext 补齐 hooks/tools 字段。

验证：
1. TurnContext 新增 hooks/hook_factories/turn_scopes/tools 字段，默认值正确
2. _build_agent_spec 接收 hooks 参数，传入 AgentTurnHookSpec.turn_hooks
3. _build_agent_spec 接收 tools 参数，覆盖默认 registry
4. _run_agent_loop 接收 hooks/tools 参数并透传
5. _state_run 从 ctx 读取四个字段并传给 _run_agent_loop

全 mock：ScriptedProvider 返回固定响应，不依赖真实 API。
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from step88.bus import MessageBus
from step88.bus.events import InboundMessage
from step88.context import ContextBuilder
from step88.hook import AgentHook, AgentHookContext
from step88.llm import LLMResponse
from step88.loop import AgentLoop, TurnContext, TurnState
from step88.memory import MemoryStore
from step88.provider import LLMProvider
from step88.session import SessionManager
from step88.tool import ToolRegistry
from step88.tools.echo import EchoTool


# ---------------------------------------------------------------------------
# Mock provider
# ---------------------------------------------------------------------------

class _ScriptedProvider(LLMProvider):
    """返回固定响应的 mock provider。"""

    def __init__(self, response: str = "Hello from mock.") -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        self.calls.append({"messages": messages, "tools": tools, **kwargs})
        return LLMResponse(content=self.response, tool_calls=[])


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _mk_loop(
    tmp_path: Path,
    provider: LLMProvider | None = None,
) -> AgentLoop:
    """构造最小可运行 AgentLoop。"""
    bus = MessageBus()
    registry = ToolRegistry()
    registry.register(EchoTool())
    return AgentLoop(
        bus=bus,
        provider=provider or _ScriptedProvider(),
        registry=registry,
        session_manager=SessionManager(workspace=str(tmp_path)),
        context_builder=ContextBuilder(workspace=str(tmp_path)),
        memory=MemoryStore(workspace=str(tmp_path)),
        identity="You are a test bot.",
        replay_budget=10_000,
    )


def _mk_msg(content: str = "hello") -> InboundMessage:
    return InboundMessage(content=content, chat_id="chat1", sender_id="user")


class _RecordingHook(AgentHook):
    """记录 before_run 调用的 hook。"""

    def __init__(self) -> None:
        super().__init__()
        self.before_run_called = False

    async def before_run(self, context: AgentHookContext) -> None:
        self.before_run_called = True


# ---------------------------------------------------------------------------
# 1. TurnContext 字段默认值
# ---------------------------------------------------------------------------

class TestTurnContextDefaults:
    """验证 TurnContext 新字段的默认值。"""

    def test_hooks_default_empty_list(self) -> None:
        ctx = TurnContext(msg=_mk_msg(), session_key="k1")
        assert ctx.hooks == []

    def test_hook_factories_default_empty_list(self) -> None:
        ctx = TurnContext(msg=_mk_msg(), session_key="k1")
        assert ctx.hook_factories == []

    def test_turn_scopes_default_empty_list(self) -> None:
        ctx = TurnContext(msg=_mk_msg(), session_key="k1")
        assert ctx.turn_scopes == []

    def test_tools_default_none(self) -> None:
        ctx = TurnContext(msg=_mk_msg(), session_key="k1")
        assert ctx.tools is None

    def test_fields_can_be_set(self) -> None:
        hook = _RecordingHook()
        custom_tools = ToolRegistry()
        ctx = TurnContext(
            msg=_mk_msg(),
            session_key="k1",
            hooks=[hook],
            hook_factories=[lambda c: hook],
            turn_scopes=[],
            tools=custom_tools,
        )
        assert ctx.hooks == [hook]
        assert len(ctx.hook_factories) == 1
        assert ctx.tools is custom_tools


# ---------------------------------------------------------------------------
# 2. _build_agent_spec hooks 参数
# ---------------------------------------------------------------------------

class TestBuildAgentSpecHooks:
    """验证 _build_agent_spec 接收 hooks 并传入 AgentTurnHookSpec.turn_hooks。"""

    def test_hooks_passed_to_turn_hooks(self, tmp_path: Path) -> None:
        loop = _mk_loop(tmp_path)
        hook = _RecordingHook()
        msg = _mk_msg()
        session = loop.sessions.get_or_create("chat1")

        spec = loop._build_agent_spec(
            channel=msg.channel,
            chat_id=msg.chat_id,
            session_key="chat1",
            session=session,
            initial_messages=[{"role": "user", "content": "hi"}],
            hooks=[hook],
        )

        # spec.hook 是 CompositeHook 或 progress hook
        # turn_hooks 应该包含我们传入的 hook
        assert spec.hook is not None
        # 如果是 CompositeHook，检查其内部 hooks
        if hasattr(spec.hook, "_hooks"):
            assert hook in spec.hook._hooks

    def test_hooks_none_uses_default(self, tmp_path: Path) -> None:
        loop = _mk_loop(tmp_path)
        msg = _mk_msg()
        session = loop.sessions.get_or_create("chat1")

        spec = loop._build_agent_spec(
            channel=msg.channel,
            chat_id=msg.chat_id,
            session_key="chat1",
            session=session,
            initial_messages=[{"role": "user", "content": "hi"}],
            hooks=None,
        )
        assert spec.hook is not None


# ---------------------------------------------------------------------------
# 3. _build_agent_spec tools 参数
# ---------------------------------------------------------------------------

class TestBuildAgentSpecTools:
    """验证 _build_agent_spec 接收 tools 并覆盖默认 registry。"""

    def test_tools_override_registry(self, tmp_path: Path) -> None:
        loop = _mk_loop(tmp_path)
        custom_tools = ToolRegistry()
        msg = _mk_msg()
        session = loop.sessions.get_or_create("chat1")

        spec = loop._build_agent_spec(
            channel=msg.channel,
            chat_id=msg.chat_id,
            session_key="chat1",
            session=session,
            initial_messages=[{"role": "user", "content": "hi"}],
            tools=custom_tools,
        )
        assert spec.tools is custom_tools

    def test_tools_none_uses_default_registry(self, tmp_path: Path) -> None:
        loop = _mk_loop(tmp_path)
        msg = _mk_msg()
        session = loop.sessions.get_or_create("chat1")

        spec = loop._build_agent_spec(
            channel=msg.channel,
            chat_id=msg.chat_id,
            session_key="chat1",
            session=session,
            initial_messages=[{"role": "user", "content": "hi"}],
            tools=None,
        )
        assert spec.tools is loop.registry


# ---------------------------------------------------------------------------
# 4. _run_agent_loop hooks/tools 透传
# ---------------------------------------------------------------------------

class TestRunAgentLoopPassthrough:
    """验证 _run_agent_loop 接收 hooks/tools 并透传给 _build_agent_spec。"""

    @pytest.mark.asyncio
    async def test_hooks_passthrough(self, tmp_path: Path) -> None:
        provider = _ScriptedProvider("ok")
        loop = _mk_loop(tmp_path, provider=provider)
        hook = _RecordingHook()
        msg = _mk_msg()
        session = loop.sessions.get_or_create("chat1")

        # spy on _build_agent_spec to capture hooks argument
        original_build = loop._build_agent_spec
        captured: dict[str, Any] = {}

        def spy_build(*args: Any, **kwargs: Any) -> Any:
            captured.update(kwargs)
            return original_build(*args, **kwargs)

        loop._build_agent_spec = spy_build  # type: ignore[method-assign]

        try:
            await loop._run_agent_loop(
                initial_messages=[{"role": "user", "content": "hi"}],
                channel=msg.channel,
                chat_id=msg.chat_id,
                metadata=msg.metadata if isinstance(msg.metadata, dict) else None,
                original_user_text=msg.content if isinstance(msg.content, str) else None,
                session=session,
                session_key="chat1",
                runtime=loop.runtime,
                hooks=[hook],
            )
        finally:
            loop._build_agent_spec = original_build  # type: ignore[method-assign]

        assert captured.get("hooks") == [hook]

    @pytest.mark.asyncio
    async def test_tools_passthrough(self, tmp_path: Path) -> None:
        provider = _ScriptedProvider("ok")
        loop = _mk_loop(tmp_path, provider=provider)
        custom_tools = ToolRegistry()
        msg = _mk_msg()
        session = loop.sessions.get_or_create("chat1")

        original_build = loop._build_agent_spec
        captured: dict[str, Any] = {}

        def spy_build(*args: Any, **kwargs: Any) -> Any:
            captured.update(kwargs)
            return original_build(*args, **kwargs)

        loop._build_agent_spec = spy_build  # type: ignore[method-assign]

        try:
            await loop._run_agent_loop(
                initial_messages=[{"role": "user", "content": "hi"}],
                channel=msg.channel,
                chat_id=msg.chat_id,
                metadata=msg.metadata if isinstance(msg.metadata, dict) else None,
                original_user_text=msg.content if isinstance(msg.content, str) else None,
                session=session,
                session_key="chat1",
                runtime=loop.runtime,
                tools=custom_tools,
            )
        finally:
            loop._build_agent_spec = original_build  # type: ignore[method-assign]

        assert captured.get("tools") is custom_tools


# ---------------------------------------------------------------------------
# 5. _state_run 从 ctx 读取字段
# ---------------------------------------------------------------------------

class TestStateRunReadsFromCtx:
    """验证 _state_run 从 ctx 读取 hooks/hook_factories/turn_scopes/tools。"""

    @pytest.mark.asyncio
    async def test_state_run_passes_ctx_fields_to_run_agent_loop(self, tmp_path: Path) -> None:
        provider = _ScriptedProvider("ok")
        loop = _mk_loop(tmp_path, provider=provider)
        hook = _RecordingHook()
        custom_tools = ToolRegistry()
        msg = _mk_msg()
        session = loop.sessions.get_or_create("chat1")

        ctx = TurnContext(
            msg=msg,
            session_key="chat1",
            state=TurnState.RUN,
            session=session,
            initial_messages=[{"role": "user", "content": "hi"}],
            runtime=loop.runtime,
            hooks=[hook],
            hook_factories=[],
            turn_scopes=[],
            tools=custom_tools,
        )

        # spy on _run_agent_loop to capture arguments
        original_run = loop._run_agent_loop
        captured: dict[str, Any] = {}

        async def spy_run(*args: Any, **kwargs: Any) -> Any:
            captured.update(kwargs)
            return await original_run(*args, **kwargs)

        loop._run_agent_loop = spy_run  # type: ignore[method-assign]

        try:
            await loop._state_run(ctx)
        finally:
            loop._run_agent_loop = original_run  # type: ignore[method-assign]

        assert captured.get("hooks") == [hook]
        assert captured.get("hook_factories") == []
        assert captured.get("turn_scopes") == []
        assert captured.get("tools") is custom_tools

    @pytest.mark.asyncio
    async def test_state_run_default_ctx_fields(self, tmp_path: Path) -> None:
        """默认 ctx（不设置新字段）时，_state_run 仍能正常工作。"""
        provider = _ScriptedProvider("ok")
        loop = _mk_loop(tmp_path, provider=provider)
        msg = _mk_msg()
        session = loop.sessions.get_or_create("chat1")

        ctx = TurnContext(
            msg=msg,
            session_key="chat1",
            state=TurnState.RUN,
            session=session,
            initial_messages=[{"role": "user", "content": "hi"}],
            runtime=loop.runtime,
            # 不设置 hooks/hook_factories/turn_scopes/tools，使用默认值
        )

        # 应该不报错，使用默认值
        await loop._state_run(ctx)
        assert ctx.final_content == "ok"
        assert ctx.stop_reason == "stop"
