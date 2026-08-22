"""step38 测试：配置层接入 max_tool_iterations（from_config 传递 max_iterations）。

全构造数据：mock provider + tmp_path；无真实 API。
覆盖：
- from_config 默认配置使用 max_tool_iterations=200；
- 配置中自定义 max_tool_iterations 生效；
- extra 覆盖 max_iterations（测试用小值）；
- 直接构造 AgentLoop 默认仍为 5（不破坏现有行为）；
- _build_agent_spec 使用配置值。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from step99.bus import MessageBus
from step99.bus.events import InboundMessage
from step99.config.schema import Config
from step99.context import ContextBuilder
from step99.loop import AgentLoop
from step99.memory import MemoryStore
from step99.session import SessionManager
from step99.tool import ToolRegistry


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class _FakeProvider:
    """仅用于 from_config 装配的最小 provider 替身。"""

    model = "mock-model"

    async def chat(
        self, messages: list[dict], tools: Any = None,
        model: str | None = None, temperature: float = 0.7, max_tokens: int = 4096,
    ) -> Any:
        raise AssertionError("should not be called in wiring tests")


def _mk_config(tmp_path: Path, **defaults: Any) -> Config:
    """构造带 workspace 的 Config，defaults 覆盖 agents.defaults。"""
    agents_defaults: dict[str, Any] = {"workspace": str(tmp_path / "ws")}
    agents_defaults.update(defaults)
    return Config.model_validate({
        "providers": {"openai": {"apiKey": "sk-test"}},
        "agents": {"defaults": agents_defaults},
    })


def _mk_msg() -> InboundMessage:
    """构造最小 InboundMessage。"""
    return InboundMessage(content="hello", chat_id="test")


# ---------------------------------------------------------------------------
# TestFromConfigMaxIterations
# ---------------------------------------------------------------------------


class TestFromConfigMaxIterations:
    """from_config 配置层接入 max_tool_iterations。"""

    def test_default_config_uses_200(self, tmp_path: Path) -> None:
        """默认配置 from_config 后 loop.max_iterations == 200（对齐 nanobot）。"""
        config = _mk_config(tmp_path)
        loop = AgentLoop.from_config(
            config, bus=MessageBus(),
            provider=_FakeProvider(),
            session_manager=SessionManager(workspace=str(tmp_path / "ws")),
            memory=MemoryStore(workspace=str(tmp_path / "ws")),
        )
        assert loop.max_iterations == 200

    def test_custom_config_overrides(self, tmp_path: Path) -> None:
        """配置中 max_tool_iterations=50 时 loop.max_iterations == 50。"""
        config = _mk_config(tmp_path, max_tool_iterations=50)
        loop = AgentLoop.from_config(
            config, bus=MessageBus(),
            provider=_FakeProvider(),
            session_manager=SessionManager(workspace=str(tmp_path / "ws")),
            memory=MemoryStore(workspace=str(tmp_path / "ws")),
        )
        assert loop.max_iterations == 50

    def test_extra_override(self, tmp_path: Path) -> None:
        """from_config(extra={"max_iterations": 3}) 时为 3（测试用小值）。"""
        config = _mk_config(tmp_path)  # 默认 200
        loop = AgentLoop.from_config(
            config, bus=MessageBus(),
            provider=_FakeProvider(),
            session_manager=SessionManager(workspace=str(tmp_path / "ws")),
            memory=MemoryStore(workspace=str(tmp_path / "ws")),
            max_iterations=3,  # extra 覆盖
        )
        assert loop.max_iterations == 3

    def test_direct_init_default_5(self, tmp_path: Path) -> None:
        """直接构造 AgentLoop() 默认 max_iterations == 5（不破坏现有行为）。"""
        bus = MessageBus()
        registry = ToolRegistry()
        loop = AgentLoop(
            bus=bus,
            provider=_FakeProvider(),
            registry=registry,
            session_manager=SessionManager(workspace=str(tmp_path)),
            context_builder=ContextBuilder(workspace=str(tmp_path)),
            memory=MemoryStore(workspace=str(tmp_path)),
            identity="test",
            replay_budget=10_000,
        )
        assert loop.max_iterations == 5

    def test_build_agent_spec_uses_config_value(self, tmp_path: Path) -> None:
        """from_config 构造的 loop 的 _build_agent_spec 使用配置值 200。"""
        config = _mk_config(tmp_path, max_tool_iterations=42)
        loop = AgentLoop.from_config(
            config, bus=MessageBus(),
            provider=_FakeProvider(),
            session_manager=SessionManager(workspace=str(tmp_path / "ws")),
            memory=MemoryStore(workspace=str(tmp_path / "ws")),
        )
        msg = _mk_msg()
        session = loop.sessions.get_or_create("test")
        spec = loop._build_agent_spec(
            channel=msg.channel,
            chat_id=msg.chat_id,
            session_key="test",
            session=session,
            initial_messages=[{"role": "user", "content": "hi"}],
        )
        assert spec.max_iterations == 42

    def test_camel_case_config_key(self, tmp_path: Path) -> None:
        """配置中 maxToolIterations（驼峰）也能正确读取。"""
        config = Config.model_validate({
            "providers": {"openai": {"apiKey": "sk-test"}},
            "agents": {"defaults": {
                "workspace": str(tmp_path / "ws"),
                "maxToolIterations": 88,
            }},
        })
        assert config.agents.defaults.max_tool_iterations == 88
        loop = AgentLoop.from_config(
            config, bus=MessageBus(),
            provider=_FakeProvider(),
            session_manager=SessionManager(workspace=str(tmp_path / "ws")),
            memory=MemoryStore(workspace=str(tmp_path / "ws")),
        )
        assert loop.max_iterations == 88
