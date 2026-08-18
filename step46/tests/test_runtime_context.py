"""step29 运行时上下文测试（A9）。

全构造数据：假 provider + tmp_path；无真实 API。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from step46.bus import MessageBus
from step46.bus.events import InboundMessage
from step46.config.schema import Config
from step46.context import ContextBuilder
from step46.loop import AgentLoop, TurnContext
from step46.memory import MemoryStore
from step46.runtime_context import (
    RUNTIME_CONTEXT_END,
    RUNTIME_CONTEXT_TAG,
    RuntimeContextBlock,
    append_runtime_context,
    normalize_runtime_context_blocks,
    resolve_runtime_context,
    wrap_runtime_context_lines,
)
from step46.session import Session, SessionManager
from step46.tool import ToolRegistry


def _mk_loop(tmp_path, **kwargs):
    """构造最小 AgentLoop（provider 用 None 即可，run 前不触碰）。"""
    bus = MessageBus()
    return AgentLoop(
        bus=bus,
        provider=None,
        registry=ToolRegistry(),
        session_manager=SessionManager(workspace=str(tmp_path)),
        context_builder=ContextBuilder(workspace=str(tmp_path)),
        memory=MemoryStore(workspace=str(tmp_path)),
        identity="You are a test bot.",
        replay_budget=10_000,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 块构造 / 规范化 / 解析
# ---------------------------------------------------------------------------


class TestRuntimeContextBlocks:
    def test_wrap_lines(self):
        wrapped = wrap_runtime_context_lines(["a", "", "b"])
        assert wrapped == f"{RUNTIME_CONTEXT_TAG}\na\nb\n{RUNTIME_CONTEXT_END}"

    def test_wrap_empty_lines(self):
        assert wrap_runtime_context_lines([]) == ""
        assert wrap_runtime_context_lines(["", "  "]) == ""

    def test_normalize_none(self):
        assert normalize_runtime_context_blocks(None) == []

    def test_normalize_single_block(self):
        # 规范化会剥除首尾空白（与 normalize 行为一致）。
        block = RuntimeContextBlock(source="clock", content="  now  ")
        assert normalize_runtime_context_blocks(block) == [
            RuntimeContextBlock(source="clock", content="now")
        ]

    def test_normalize_filters_empty_content(self):
        blocks = normalize_runtime_context_blocks([
            RuntimeContextBlock(source="a", content="x"),
            RuntimeContextBlock(source="b", content="   "),
        ])
        assert [b.source for b in blocks] == ["a"]

    def test_normalize_rejects_empty_source(self):
        with pytest.raises(ValueError):
            normalize_runtime_context_blocks(RuntimeContextBlock(source=" ", content="x"))

    def test_normalize_rejects_non_block(self):
        with pytest.raises(TypeError):
            normalize_runtime_context_blocks(["not a block"])

    def test_resolve_sequential_order(self):
        async def run():
            calls: list[str] = []

            async def p1(request):
                calls.append("p1")
                return RuntimeContextBlock(source="p1", content="one")

            async def p2(request):
                calls.append("p2")
                return [
                    RuntimeContextBlock(source="p2a", content="two"),
                    RuntimeContextBlock(source="p2b", content="three"),
                ]

            async def p3(request):
                calls.append("p3")
                return None

            blocks = await resolve_runtime_context([p1, p2, p3], request=None)
            return calls, blocks

        calls, blocks = run_async(run())
        assert calls == ["p1", "p2", "p3"]
        assert [b.source for b in blocks] == ["p1", "p2a", "p2b"]


# ---------------------------------------------------------------------------
# append_runtime_context：文本 / 多模态两种形态
# ---------------------------------------------------------------------------


class TestAppendRuntimeContext:
    def test_no_blocks_passthrough(self):
        content, marker = append_runtime_context("hi", [])
        assert content == "hi"
        assert marker is None

    def test_text_form_appends_suffix(self):
        content, marker = append_runtime_context(
            "hello", [RuntimeContextBlock(source="c", content="now=2026")],
        )
        assert content == "hello\n\nnow=2026"
        assert marker["version"] == 1
        assert marker["sources"] == ["c"]
        assert marker["suffix"] == "now=2026"

    def test_text_form_empty_content(self):
        content, marker = append_runtime_context(
            "", [RuntimeContextBlock(source="c", content="only")],
        )
        assert content == "only"

    def test_list_form_appends_text_blocks(self):
        content, marker = append_runtime_context(
            [{"type": "image_url", "image_url": {"url": "data:x"}}],
            [RuntimeContextBlock(source="c", content="extra")],
        )
        assert content[-1] == {"type": "text", "text": "extra"}
        assert marker["blocks"] == [{"type": "text", "text": "extra"}]


# ---------------------------------------------------------------------------
# ContextBuilder：build_messages 附加运行时上下文
# ---------------------------------------------------------------------------


class TestContextBuilderRuntimeContext:
    def _builder(self, tmp_path):
        return ContextBuilder(workspace=str(tmp_path))

    def test_blocks_appended_to_user_tail(self, tmp_path):
        builder = self._builder(tmp_path)
        messages = builder.build_messages(
            current_message="hi",
            runtime_context_blocks=[RuntimeContextBlock(source="clock", content="now=1")],
        )
        assert messages[-1]["role"] == "user"
        assert messages[-1]["content"] == "hi\n\nnow=1"

    def test_blocks_skipped_for_assistant_role(self, tmp_path):
        builder = self._builder(tmp_path)
        messages = builder.build_messages(
            current_message="",
            current_role="assistant",
            runtime_context_blocks=[RuntimeContextBlock(source="clock", content="now=1")],
        )
        assert messages[-1]["role"] == "assistant"
        assert messages[-1]["content"] == ""

    def test_blocks_merged_into_tail_user(self, tmp_path):
        # 历史末尾已是 user（续跑场景）：合并而不是追加。
        builder = self._builder(tmp_path)
        messages = builder.build_messages(
            current_message="more",
            history=[{"role": "user", "content": "prev"}],
            runtime_context_blocks=[RuntimeContextBlock(source="c", content="now=2")],
        )
        assert messages[-1]["role"] == "user"
        assert messages[-1]["content"] == "prev\nmore\n\nnow=2"
        assert len(messages) == 2  # system + merged tail

    def test_no_blocks_unaffected(self, tmp_path):
        builder = self._builder(tmp_path)
        messages = builder.build_messages(current_message="plain")
        assert messages[-1]["content"] == "plain"

    def test_workspace_param_controls_bootstrap(self, tmp_path):
        project = tmp_path / "proj"
        project.mkdir()
        (project / "AGENTS.md").write_text("project rules", encoding="utf-8")
        builder = ContextBuilder(workspace=str(tmp_path))
        system = builder.build_system_prompt(workspace=project)
        assert "project rules" in system
        assert "project rules" not in builder.build_system_prompt()


# ---------------------------------------------------------------------------
# loop 集成：provider 注册 / 解析 / 仅内存不持久化
# ---------------------------------------------------------------------------


class TestLoopRuntimeContext:
    @pytest.mark.asyncio
    async def test_register_provider_and_resolve_for_turn(self, tmp_path):
        loop = _mk_loop(tmp_path)
        seen: dict[str, object] = {}

        async def provider(request):
            seen["session_key"] = request.session_key
            seen["workspace"] = request.workspace
            return RuntimeContextBlock(source="clock", content="now=2026")

        loop.register_runtime_context_provider(provider)
        msg = InboundMessage(content="hello", chat_id="chat1", sender_id="user")
        session = Session(key="chat1")
        ctx = TurnContext(msg=msg, session_key="chat1", session=session)
        ctx.request_context = loop._build_turn_request_context(
            msg, session, "chat1", turn_id="t1",
        )
        blocks = await loop._resolve_runtime_context_for_turn(ctx)
        assert [b.source for b in blocks] == ["clock"]
        assert seen["session_key"] == "chat1"
        assert seen["workspace"] == Path(tmp_path).resolve()

    @pytest.mark.asyncio
    async def test_register_deduplicates(self, tmp_path):
        loop = _mk_loop(tmp_path)

        async def provider(request):
            return None

        loop.register_runtime_context_provider(provider)
        loop.register_runtime_context_provider(provider)
        assert len(loop._runtime_context_providers) == 1

    @pytest.mark.asyncio
    async def test_state_build_persists_blocks_in_history(self, tmp_path):
        """step34：对齐 nanobot，运行时上下文同时写入 initial_messages 和持久化历史。

        step33 及之前：runtime 上下文只拼进内存 initial_messages，不写入会话历史。
        step34 起：_persist_user_message_early 持久化含运行时上下文 + marker 的
        用户消息，这样下一轮历史回放时 LLM 能看到上一轮的运行时上下文。
        """
        loop = _mk_loop(tmp_path)

        async def provider(request):
            return RuntimeContextBlock(source="clock", content="now=2026")

        loop.register_runtime_context_provider(provider)
        msg = InboundMessage(content="hello", chat_id="chat1", sender_id="user")
        session = Session(key="chat1")
        ctx = TurnContext(msg=msg, session_key="chat1", session=session, turn_id="t1")
        await loop._state_build(ctx)

        assert ctx.initial_messages[-1]["role"] == "user"
        assert "now=2026" in ctx.initial_messages[-1]["content"]
        # step34：持久化的用户消息也包含运行时上下文（对齐 nanobot）。
        assert "now=2026" in ctx.session.messages[-1]["content"]
        assert ctx.session.messages[-1]["content"] == ctx.initial_messages[-1]["content"]
        # 持久化消息包含 RUNTIME_CONTEXT_HISTORY_META marker。
        from step46.runtime_context import RUNTIME_CONTEXT_HISTORY_META
        assert RUNTIME_CONTEXT_HISTORY_META in ctx.session.messages[-1]

    @pytest.mark.asyncio
    async def test_tool_provider_collected_via_registry(self, tmp_path):
        loop = _mk_loop(tmp_path)

        class _ToolWithProvider:
            """带 runtime_context_provider 的假工具。"""

            name = "probe"

            def runtime_context_provider(self):
                async def provider(request):
                    return RuntimeContextBlock(source="goal", content="state=active")

                return provider

        registry = loop.registry
        registry.register(_ToolWithProvider())  # type: ignore[arg-type]

        msg = InboundMessage(content="hi", chat_id="chat1", sender_id="user")
        session = Session(key="chat1")
        ctx = TurnContext(msg=msg, session_key="chat1", session=session)
        ctx.request_context = loop._build_turn_request_context(msg, session, "chat1")
        blocks = await loop._resolve_runtime_context_for_turn(ctx)
        assert [b.source for b in blocks] == ["goal"]

    @pytest.mark.asyncio
    async def test_workspace_restriction_wiring(self, tmp_path):
        loop = _mk_loop(tmp_path, restrict_to_workspace=True)
        scope = loop.workspace_scopes.default()
        assert scope.restrict_to_workspace is True
        assert scope.project_path == Path(tmp_path).resolve()
        # config 贯通：tools.restrict_to_workspace → AgentLoop.from_config
        config = Config()
        config.tools.restrict_to_workspace = True
        built = AgentLoop.from_config(
            config,
            bus=MessageBus(),
            provider=_FakeProvider(),
            session_manager=SessionManager(workspace=str(tmp_path)),
            memory=MemoryStore(workspace=str(tmp_path)),
        )
        assert built.restrict_to_workspace is True


class _FakeProvider:
    """仅用于 from_config 装配的最小 provider 替身。"""

    model = "mock-model"

    async def chat(self, messages, tools=None, model=None, temperature=0.7, max_tokens=4096):
        raise AssertionError("should not be called in wiring tests")


def run_async(coro):
    import asyncio

    return asyncio.run(coro)
