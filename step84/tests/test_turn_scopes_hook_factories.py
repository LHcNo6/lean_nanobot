"""step40 测试：turn_scopes + hook_factories。

全 mock：ScriptedProvider 返回固定响应；hook 工厂创建记录调用的 hook；
turn_scopes 用记录 enter/exit 的 context manager。
覆盖：
- hook_factories：AgentLoop 级（registered）和 turn 级工厂被调用；
- hook 工厂返回 None / 抛异常时跳过；
- turn_scopes：enter/exit 生命周期、多 scope 顺序、异常时仍退出。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from step84.bus import MessageBus
from step84.bus.events import InboundMessage
from step84.context import ContextBuilder
from step84.hook import AgentHook, AgentHookContext, AgentTurnHookContext
from step84.llm import LLMResponse
from step84.loop import AgentLoop
from step84.memory import MemoryStore
from step84.provider import LLMProvider
from step84.session import SessionManager
from step84.tool import ToolRegistry
from step84.tools.echo import EchoTool


# ---------------------------------------------------------------------------
# mock provider & helpers
# ---------------------------------------------------------------------------


class _ScriptedProvider(LLMProvider):
    """按脚本队列返回响应，记录每次调用。"""

    def __init__(self, script: list[LLMResponse] | None = None):
        super().__init__()
        self._script = list(script or [])
        self.calls = 0

    @property
    def model(self) -> str:
        return "mock-model"

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: Any = None,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        self.calls += 1
        if self._script:
            return self._script.pop(0)
        return LLMResponse(content="done", finish_reason="stop")


class _BoomProvider(LLMProvider):
    """每次调用都抛异常。"""

    @property
    def model(self) -> str:
        return "boom-model"

    async def chat(self, messages, tools=None, **kwargs) -> LLMResponse:
        raise RuntimeError("provider boom")


def _mk_loop(
    tmp_path: Path,
    provider: LLMProvider | None = None,
    hook_factories: list | None = None,
) -> AgentLoop:
    """构造最小可运行 AgentLoop。"""
    bus = MessageBus()
    registry = ToolRegistry()
    registry.register(EchoTool())
    kwargs: dict[str, Any] = dict(
        bus=bus,
        provider=provider or _ScriptedProvider(),
        registry=registry,
        session_manager=SessionManager(workspace=str(tmp_path)),
        context_builder=ContextBuilder(workspace=str(tmp_path)),
        memory=MemoryStore(workspace=str(tmp_path)),
        identity="You are a test bot.",
        replay_budget=10_000,
    )
    if hook_factories is not None:
        kwargs["hook_factories"] = hook_factories
    return AgentLoop(**kwargs)


def _mk_msg(content: str = "hello") -> InboundMessage:
    return InboundMessage(content=content, chat_id="chat1", sender_id="user")


# ---------------------------------------------------------------------------
# 辅助：记录调用的 hook
# ---------------------------------------------------------------------------


class _RecordingHook(AgentHook):
    """记录 before_run / after_run 调用次数。"""

    def __init__(self, name: str) -> None:
        super().__init__()
        self.name = name
        self.before_run_calls = 0
        self.after_run_calls = 0

    async def before_run(self, context: AgentHookContext) -> None:
        self.before_run_calls += 1

    async def after_run(self, context: AgentHookContext) -> None:
        self.after_run_calls += 1


# ---------------------------------------------------------------------------
# 辅助：记录 enter/exit 的 scope
# ---------------------------------------------------------------------------


class _RecordingScope:
    """记录 enter/exit 顺序的 context manager。"""

    def __init__(self, name: str, log: list[str]) -> None:
        self.name = name
        self.log = log
        self.entered = False
        self.exited = False

    def __enter__(self) -> "_RecordingScope":
        self.entered = True
        self.log.append(f"enter:{self.name}")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        self.exited = True
        self.log.append(f"exit:{self.name}")
        return False


# ---------------------------------------------------------------------------
# TestHookFactories
# ---------------------------------------------------------------------------


class TestHookFactories:
    """hook_factories 装配。"""

    def test_agent_loop_stores_hook_factories(self, tmp_path: Path) -> None:
        """AgentLoop.__init__ 存储 hook_factories。"""
        def factory(ctx: AgentTurnHookContext) -> AgentHook | None:
            return _RecordingHook("from-factory")

        loop = _mk_loop(tmp_path, hook_factories=[factory])
        assert loop._hook_factories == [factory]

    def test_registered_hook_factory_applied(self, tmp_path: Path) -> None:
        """AgentLoop 级 hook 工厂创建的 hook 被调用（before_run）。"""
        created_hook = _RecordingHook("registered")

        def factory(ctx: AgentTurnHookContext) -> AgentHook | None:
            return created_hook

        loop = _mk_loop(tmp_path, hook_factories=[factory])
        msg = _mk_msg()

        asyncio.run(loop._run_agent_loop(
            initial_messages=[{"role": "user", "content": "hi"}],
            channel=msg.channel,
            chat_id=msg.chat_id,
            metadata=msg.metadata if isinstance(msg.metadata, dict) else None,
            original_user_text=msg.content if isinstance(msg.content, str) else None,
            session=None,
            session_key="s1",
            runtime=loop.runtime,
        ))

        assert created_hook.before_run_calls == 1
        assert created_hook.after_run_calls == 1

    def test_turn_hook_factory_applied(self, tmp_path: Path) -> None:
        """turn 级 hook 工厂创建的 hook 被调用。"""
        created_hook = _RecordingHook("turn")

        def factory(ctx: AgentTurnHookContext) -> AgentHook | None:
            return created_hook

        loop = _mk_loop(tmp_path)
        msg = _mk_msg()

        asyncio.run(loop._run_agent_loop(
            initial_messages=[{"role": "user", "content": "hi"}],
            channel=msg.channel,
            chat_id=msg.chat_id,
            metadata=msg.metadata if isinstance(msg.metadata, dict) else None,
            original_user_text=msg.content if isinstance(msg.content, str) else None,
            session=None,
            session_key="s1",
            runtime=loop.runtime,
            hook_factories=[factory],
        ))

        assert created_hook.before_run_calls == 1

    def test_hook_factory_returns_none_skipped(self, tmp_path: Path) -> None:
        """工厂返回 None 时跳过，不影响运行。"""
        def factory(ctx: AgentTurnHookContext) -> AgentHook | None:
            return None

        loop = _mk_loop(tmp_path, hook_factories=[factory])
        msg = _mk_msg()

        result = asyncio.run(loop._run_agent_loop(
            initial_messages=[{"role": "user", "content": "hi"}],
            channel=msg.channel,
            chat_id=msg.chat_id,
            metadata=msg.metadata if isinstance(msg.metadata, dict) else None,
            original_user_text=msg.content if isinstance(msg.content, str) else None,
            session=None,
            session_key="s1",
            runtime=loop.runtime,
        ))

        assert result[3] == "stop"  # stop_reason

    def test_hook_factory_exception_skipped(self, tmp_path: Path) -> None:
        """工厂抛异常时跳过，不崩溃。"""
        def bad_factory(ctx: AgentTurnHookContext) -> AgentHook | None:
            raise RuntimeError("factory boom")

        loop = _mk_loop(tmp_path, hook_factories=[bad_factory])
        msg = _mk_msg()

        result = asyncio.run(loop._run_agent_loop(
            initial_messages=[{"role": "user", "content": "hi"}],
            channel=msg.channel,
            chat_id=msg.chat_id,
            metadata=msg.metadata if isinstance(msg.metadata, dict) else None,
            original_user_text=msg.content if isinstance(msg.content, str) else None,
            session=None,
            session_key="s1",
            runtime=loop.runtime,
        ))

        assert result[3] == "stop"

    def test_registered_factory_before_turn_factory(self, tmp_path: Path) -> None:
        """registered 工厂先于 turn 工厂执行（hook 链顺序）。"""
        order: list[str] = []

        def reg_factory(ctx: AgentTurnHookContext) -> AgentHook | None:
            order.append("reg")
            return _RecordingHook("reg")

        def turn_factory(ctx: AgentTurnHookContext) -> AgentHook | None:
            order.append("turn")
            return _RecordingHook("turn")

        loop = _mk_loop(tmp_path, hook_factories=[reg_factory])
        msg = _mk_msg()

        asyncio.run(loop._run_agent_loop(
            initial_messages=[{"role": "user", "content": "hi"}],
            channel=msg.channel,
            chat_id=msg.chat_id,
            metadata=msg.metadata if isinstance(msg.metadata, dict) else None,
            original_user_text=msg.content if isinstance(msg.content, str) else None,
            session=None,
            session_key="s1",
            runtime=loop.runtime,
            hook_factories=[turn_factory],
        ))

        assert order == ["reg", "turn"]


# ---------------------------------------------------------------------------
# TestTurnScopes
# ---------------------------------------------------------------------------


class TestTurnScopes:
    """turn_scopes 生命周期。"""

    def test_turn_scope_entered_and_exited(self, tmp_path: Path) -> None:
        """scope 的 __enter__ 和 __exit__ 被调用。"""
        log: list[str] = []
        scope = _RecordingScope("s1", log)

        loop = _mk_loop(tmp_path)
        msg = _mk_msg()

        asyncio.run(loop._run_agent_loop(
            initial_messages=[{"role": "user", "content": "hi"}],
            channel=msg.channel,
            chat_id=msg.chat_id,
            metadata=msg.metadata if isinstance(msg.metadata, dict) else None,
            original_user_text=msg.content if isinstance(msg.content, str) else None,
            session=None,
            session_key="s1",
            runtime=loop.runtime,
            turn_scopes=[scope],
        ))

        assert scope.entered is True
        assert scope.exited is True
        assert log == ["enter:s1", "exit:s1"]

    def test_multiple_turn_scopes_order(self, tmp_path: Path) -> None:
        """多个 scope 按顺序进入，逆序退出。"""
        log: list[str] = []
        s1 = _RecordingScope("s1", log)
        s2 = _RecordingScope("s2", log)

        loop = _mk_loop(tmp_path)
        msg = _mk_msg()

        asyncio.run(loop._run_agent_loop(
            initial_messages=[{"role": "user", "content": "hi"}],
            channel=msg.channel,
            chat_id=msg.chat_id,
            metadata=msg.metadata if isinstance(msg.metadata, dict) else None,
            original_user_text=msg.content if isinstance(msg.content, str) else None,
            session=None,
            session_key="s1",
            runtime=loop.runtime,
            turn_scopes=[s1, s2],
        ))

        # ExitStack: enter s1, enter s2; exit s2, exit s1
        assert log == ["enter:s1", "enter:s2", "exit:s2", "exit:s1"]

    def test_turn_scopes_none_no_error(self, tmp_path: Path) -> None:
        """turn_scopes=None 时不报错。"""
        loop = _mk_loop(tmp_path)
        msg = _mk_msg()

        result = asyncio.run(loop._run_agent_loop(
            initial_messages=[{"role": "user", "content": "hi"}],
            channel=msg.channel,
            chat_id=msg.chat_id,
            metadata=msg.metadata if isinstance(msg.metadata, dict) else None,
            original_user_text=msg.content if isinstance(msg.content, str) else None,
            session=None,
            session_key="s1",
            runtime=loop.runtime,
            turn_scopes=None,
        ))

        assert result[3] == "stop"

    def test_turn_scope_empty_list_no_error(self, tmp_path: Path) -> None:
        """turn_scopes=[] 时不报错。"""
        loop = _mk_loop(tmp_path)
        msg = _mk_msg()

        result = asyncio.run(loop._run_agent_loop(
            initial_messages=[{"role": "user", "content": "hi"}],
            channel=msg.channel,
            chat_id=msg.chat_id,
            metadata=msg.metadata if isinstance(msg.metadata, dict) else None,
            original_user_text=msg.content if isinstance(msg.content, str) else None,
            session=None,
            session_key="s1",
            runtime=loop.runtime,
            turn_scopes=[],
        ))

        assert result[3] == "stop"

    def test_turn_scope_exited_on_runner_exception(self, tmp_path: Path) -> None:
        """runner 抛异常时 scope 仍被退出（finally 保证）。"""
        log: list[str] = []
        scope = _RecordingScope("s1", log)

        loop = _mk_loop(tmp_path, provider=_BoomProvider())
        msg = _mk_msg()

        with pytest.raises(RuntimeError, match="provider boom"):
            asyncio.run(loop._run_agent_loop(
                initial_messages=[{"role": "user", "content": "hi"}],
                channel=msg.channel,
                chat_id=msg.chat_id,
                metadata=msg.metadata if isinstance(msg.metadata, dict) else None,
                original_user_text=msg.content if isinstance(msg.content, str) else None,
                session=None,
                session_key="s1",
                runtime=loop.runtime,
                turn_scopes=[scope],
            ))

        assert scope.entered is True
        assert scope.exited is True
        assert log == ["enter:s1", "exit:s1"]

    def test_turn_scope_with_hook_factories_combined(self, tmp_path: Path) -> None:
        """turn_scopes 和 hook_factories 可同时使用。"""
        log: list[str] = []
        scope = _RecordingScope("s1", log)
        created_hook = _RecordingHook("combined")

        def factory(ctx: AgentTurnHookContext) -> AgentHook | None:
            return created_hook

        loop = _mk_loop(tmp_path)
        msg = _mk_msg()

        asyncio.run(loop._run_agent_loop(
            initial_messages=[{"role": "user", "content": "hi"}],
            channel=msg.channel,
            chat_id=msg.chat_id,
            metadata=msg.metadata if isinstance(msg.metadata, dict) else None,
            original_user_text=msg.content if isinstance(msg.content, str) else None,
            session=None,
            session_key="s1",
            runtime=loop.runtime,
            hook_factories=[factory],
            turn_scopes=[scope],
        ))

        assert scope.exited is True
        assert created_hook.before_run_calls == 1
