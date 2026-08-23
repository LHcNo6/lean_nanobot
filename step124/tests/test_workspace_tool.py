"""step29 workspace 工具端测试（A10 消费端 + runner 绑定）。

全构造数据：tmp_path 真实文件 + 脚本化 provider；无真实 API。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from step124.bus import MessageBus
from step124.bus.events import InboundMessage
from step124.context import ContextBuilder, ToolContext
from step124.llm import LLMResponse, ToolCallRequest
from step124.loop import AgentLoop
from step124.memory import MemoryStore
from step124.provider import LLMProvider
from step124.runner import AgentRunSpec, AgentRunner
from step124.security.workspace_access import (
    current_tool_workspace,
    current_workspace_scope,
    default_workspace_scope,
)
from step124.session import Session, SessionManager
from step124.skills.loader import BUILTIN_SKILLS_DIR
from step124.tool import Tool, ToolRegistry, ToolResult
from step124.tools.read_file import ReadFileTool


def _mk_loop(tmp_path, *, restrict_to_workspace: bool = False):
    return AgentLoop(
        bus=MessageBus(),
        provider=None,
        registry=ToolRegistry(),
        session_manager=SessionManager(workspace=str(tmp_path)),
        context_builder=ContextBuilder(workspace=str(tmp_path)),
        memory=MemoryStore(workspace=str(tmp_path)),
        identity="You are a test bot.",
        replay_budget=10_000,
        restrict_to_workspace=restrict_to_workspace,
    )


# ---------------------------------------------------------------------------
# ToolContext：真实 workspace / restrict 意图
# ---------------------------------------------------------------------------


class TestReadFileToolContext:
    def test_create_from_context(self, tmp_path):
        tool = ReadFileTool.create(
            ToolContext(workspace=str(tmp_path), restrict_to_workspace=True)
        )
        assert isinstance(tool, ReadFileTool)
        assert tool._workspace == str(tmp_path)
        assert tool._restrict is True

    def test_create_without_context_falls_back(self):
        tool = ReadFileTool.create(None)
        assert tool._workspace == ""
        assert tool._restrict is False

    def test_read_only_flag(self, tmp_path):
        tool = ReadFileTool(workspace=str(tmp_path))
        assert tool.read_only is True
        assert tool.concurrency_safe is True


# ---------------------------------------------------------------------------
# read_file：边界强制执行
# ---------------------------------------------------------------------------


class TestReadFileBoundary:
    @pytest.mark.asyncio
    async def test_read_inside_workspace(self, tmp_path):
        f = tmp_path / "note.txt"
        f.write_text("hello world", encoding="utf-8")
        tool = ReadFileTool(workspace=str(tmp_path), restrict_to_workspace=True)
        result = await tool.execute(path=str(f))
        assert "hello world" in result

    @pytest.mark.asyncio
    async def test_relative_path_resolved_against_workspace(self, tmp_path):
        (tmp_path / "note.txt").write_text("rel ok", encoding="utf-8")
        tool = ReadFileTool(workspace=str(tmp_path), restrict_to_workspace=True)
        result = await tool.execute(path="note.txt")
        assert "rel ok" in result

    @pytest.mark.asyncio
    async def test_outside_workspace_rejected(self, tmp_path):
        outside = tmp_path.parent / "secret.txt"
        outside.write_text("top secret", encoding="utf-8")
        tool = ReadFileTool(workspace=str(tmp_path), restrict_to_workspace=True)
        result = await tool.execute(path=str(outside))
        assert result.is_error
        assert "boundary" in result.lower() or "outside allowed" in result.lower()

    @pytest.mark.asyncio
    async def test_parent_traversal_rejected(self, tmp_path):
        (tmp_path.parent / "secret.txt").write_text("top secret", encoding="utf-8")
        tool = ReadFileTool(workspace=str(tmp_path), restrict_to_workspace=True)
        result = await tool.execute(path="../secret.txt")
        assert result.is_error

    @pytest.mark.asyncio
    async def test_full_mode_allows_outside(self, tmp_path):
        outside = tmp_path.parent / "secret.txt"
        outside.write_text("public", encoding="utf-8")
        tool = ReadFileTool(workspace=str(tmp_path), restrict_to_workspace=False)
        result = await tool.execute(path=str(outside))
        assert "public" in result

    @pytest.mark.asyncio
    async def test_skills_dir_exempt_while_restricted(self, tmp_path):
        # 内置技能目录豁免：受限时仍可读 SKILL.md（对齐 nanobot extra_read）。
        skill_file = BUILTIN_SKILLS_DIR / "memory" / "SKILL.md"
        if not skill_file.is_file():
            pytest.skip("builtin skills dir missing")
        tool = ReadFileTool(workspace=str(tmp_path), restrict_to_workspace=True)
        result = await tool.execute(path=str(skill_file))
        assert skill_file.read_text(encoding="utf-8")[:40] in result

    @pytest.mark.asyncio
    async def test_max_chars_truncation(self, tmp_path):
        f = tmp_path / "big.txt"
        f.write_text("x" * 5000, encoding="utf-8")
        tool = ReadFileTool(workspace=str(tmp_path))
        result = await tool.execute(path=str(f), max_chars=100)
        assert "truncated" in result
        assert len(result) < 200

    @pytest.mark.asyncio
    async def test_missing_file_error(self, tmp_path):
        tool = ReadFileTool(workspace=str(tmp_path))
        result = await tool.execute(path="nope.txt")
        assert result.is_error
        assert "not found" in result.lower()

    @pytest.mark.asyncio
    async def test_missing_path_parameter(self, tmp_path):
        tool = ReadFileTool(workspace=str(tmp_path))
        result = await tool.execute()
        assert result.is_error


# ---------------------------------------------------------------------------
# runner 绑定：spec.request_context / workspace_scope
# ---------------------------------------------------------------------------


class _ProbeTool(Tool):
    """记录执行时查询到的工作区信息的探针工具。"""

    def __init__(self, sink: dict) -> None:
        self._sink = sink

    @property
    def name(self) -> str:
        return "probe"

    @property
    def description(self) -> str:
        return "records tool context for tests"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {"text": {"type": "string"}}}

    async def execute(self, text: str = "", **kwargs) -> ToolResult:
        from step124.context import current_request_context
        from step124.security.workspace_access import current_workspace_scope

        request = current_request_context()
        scope = current_workspace_scope()
        access = current_tool_workspace("", restrict_to_workspace=False)
        self._sink.update({
            "request_workspace": request.workspace if request else None,
            "request_turn": request.turn_id if request else None,
            "scope_is_bound": scope is not None,
            "tool_workspace": access.project_path,
            "tool_restrict": access.restrict_to_workspace,
        })
        return ToolResult("probed")


class _ScriptedProvider(LLMProvider):
    """按脚本回放响应（先工具调用、后最终回复）。"""

    def __init__(self, *responses: LLMResponse):
        super().__init__()
        self._responses = list(responses)

    @property
    def model(self) -> str:
        return "mock-model"

    async def chat(self, messages, tools=None, model=None, temperature=0.7, max_tokens=4096):
        assert self._responses, "provider script exhausted"
        return self._responses.pop(0)


def _mk_probe_spec(sink: dict, tmp_path, *, with_context: bool = True):
    registry = ToolRegistry()
    registry.register(_ProbeTool(sink))
    responses = [
        LLMResponse(
            content="",
            tool_calls=[ToolCallRequest(id="call_probe", name="probe", arguments={"text": "x"})],
            finish_reason="tool_calls",
        ),
        LLMResponse(content="done"),
    ]
    spec = AgentRunSpec(
        initial_messages=[{"role": "user", "content": "go"}],
        tools=registry,
        provider=_ScriptedProvider(*responses),
        max_iterations=3,
        concurrent_tools=False,
    )
    scope = default_workspace_scope(tmp_path, True)
    if with_context:
        spec.workspace_scope = scope
        from step124.context import RequestContext

        spec.request_context = RequestContext(
            channel="cli", chat_id="chat1", session_key="chat1",
            turn_id="t-probe", workspace=scope.project_path,
        )
    return scope, spec


class TestRunnerBinding:
    @pytest.mark.asyncio
    async def test_rich_context_bound_during_run_and_restored(self, tmp_path):
        sink: dict = {}
        scope, spec = _mk_probe_spec(sink, tmp_path)
        result = await AgentRunner().run(spec)
        assert result.stop_reason == "completed" or result.final_content == "done"
        assert sink["request_workspace"] == scope.project_path
        assert sink["request_turn"] == "t-probe"
        assert sink["scope_is_bound"] is True
        assert sink["tool_workspace"] == scope.project_path
        assert sink["tool_restrict"] is True

    @pytest.mark.asyncio
    async def test_scope_restored_after_run(self, tmp_path):
        sink: dict = {}
        _, spec = _mk_probe_spec(sink, tmp_path)
        assert current_workspace_scope() is None
        await AgentRunner().run(spec)
        assert current_workspace_scope() is None
        from step124.context import current_request_context

        assert current_request_context() is None

    @pytest.mark.asyncio
    async def test_fallback_minimal_context_without_spec_fields(self, tmp_path):
        sink: dict = {}
        _, spec = _mk_probe_spec(sink, tmp_path, with_context=False)
        await AgentRunner().run(spec)
        assert sink["request_workspace"] is None
        assert sink["scope_is_bound"] is False


# ---------------------------------------------------------------------------
# loop 装配：_build_agent_spec → ToolContext 真值 → read_file 自动注册
# ---------------------------------------------------------------------------


class TestLoopToolAssembly:
    def test_spec_carries_scope_and_request_context(self, tmp_path):
        loop = _mk_loop(tmp_path, restrict_to_workspace=True)
        session = Session(key="chat1")
        msg = InboundMessage(content="hi", chat_id="chat1", sender_id="user")
        spec = loop._build_agent_spec(
            channel=msg.channel,
            chat_id=msg.chat_id,
            session_key="chat1",
            session=session,
            initial_messages=[{"role": "user", "content": "hi"}],
        )
        assert spec.workspace_scope is not None
        assert spec.workspace_scope.restrict_to_workspace is True
        assert spec.workspace_scope.project_path == Path(tmp_path).resolve()
        assert spec.request_context is None or spec.request_context.workspace is not None

    def test_read_file_registered_with_real_workspace(self, tmp_path):
        loop = _mk_loop(tmp_path, restrict_to_workspace=True)
        session = Session(key="chat1")
        msg = InboundMessage(content="hi", chat_id="chat1", sender_id="user")
        loop._build_agent_spec(
            channel=msg.channel,
            chat_id=msg.chat_id,
            session_key="chat1",
            session=session,
            initial_messages=[{"role": "user", "content": "hi"}],
        )
        tool = loop.registry.get("read_file")
        assert tool is not None
        assert tool._workspace == str(Path(tmp_path).resolve())
        assert tool._restrict is True

    def test_unrestricted_loop_loads_read_file_open(self, tmp_path):
        loop = _mk_loop(tmp_path, restrict_to_workspace=False)
        session = Session(key="chat1")
        msg = InboundMessage(content="hi", chat_id="chat1", sender_id="user")
        loop._build_agent_spec(channel=msg.channel, chat_id=msg.chat_id, session_key="chat1", session=session, initial_messages=[{"role": "user", "content": "hi"}])
        tool = loop.registry.get("read_file")
        assert tool is not None
        assert tool._restrict is False