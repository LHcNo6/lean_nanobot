"""step81：WebSearch 多 provider 单元测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from step101.context import ToolContext
from step101.tools.web import (
    BraveProvider,
    DuckDuckGoProvider,
    SearxngProvider,
    TavilyProvider,
    WebSearchTool,
    _create_provider,
)


def _make_config(*, provider: str = "duckduckgo", api_key: str = "", base_url: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        exec=SimpleNamespace(enable=True, timeout=60, sandbox="", allowed_env_keys=[], allow_patterns=[], deny_patterns=[], path_prepend="", path_append=""),
        tools=SimpleNamespace(restrict_to_workspace=False, file=SimpleNamespace(enable=True)),
        web=SimpleNamespace(
            enable=True, timeout=30, user_agent="Test",
            search=SimpleNamespace(provider=provider, max_results=5, timeout=30, api_key=api_key, base_url=base_url),
        ),
        my=SimpleNamespace(enable=True, allow_set=False),
        image_generation=SimpleNamespace(enabled=True, provider="simple", save_dir="generated"),
    )


def _make_ctx(workspace: str, **kwargs) -> ToolContext:
    from step101.tools.file_state import FileStateStore
    from step101.tools.cron import _CronStore
    return ToolContext(
        config=_make_config(**kwargs),
        workspace=workspace,
        restrict_to_workspace=False,
        session_key="test-session",
        file_state_store=FileStateStore(),
        cron_store=_CronStore(),
    )


def _run(coro):
    return asyncio.run(coro)


class TestProviderFactory:
    """provider 工厂。"""

    def test_create_duckduckgo(self) -> None:
        """创建 duckduckgo provider。"""
        p = _create_provider("duckduckgo", 30, "Test")
        assert isinstance(p, DuckDuckGoProvider)

    def test_create_brave(self) -> None:
        """创建 brave provider。"""
        p = _create_provider("brave", 30, "Test", api_key="key123")
        assert isinstance(p, BraveProvider)

    def test_create_tavily(self) -> None:
        """创建 tavily provider。"""
        p = _create_provider("tavily", 30, "Test", api_key="key123")
        assert isinstance(p, TavilyProvider)

    def test_create_searxng(self) -> None:
        """创建 searxng provider。"""
        p = _create_provider("searxng", 30, "Test", base_url="http://localhost:8080")
        assert isinstance(p, SearxngProvider)

    def test_unknown_provider_error(self) -> None:
        """未知 provider 报错。"""
        with pytest.raises(ValueError):
            _create_provider("unknown", 30, "Test")

    def test_case_insensitive(self) -> None:
        """provider 名称不区分大小写。"""
        p = _create_provider("DuckDuckGo", 30, "Test")
        assert isinstance(p, DuckDuckGoProvider)


class TestBraveProvider:
    """BraveProvider。"""

    def test_requires_api_key(self) -> None:
        """没有 api_key 时报错。"""
        p = BraveProvider(api_key="", timeout=30, user_agent="Test")
        with pytest.raises(ValueError, match="API key"):
            _run(p.search("test", 5))

    def test_has_api_url(self) -> None:
        """有 API URL。"""
        assert "api.search.brave.com" in BraveProvider._API_URL


class TestTavilyProvider:
    """TavilyProvider。"""

    def test_requires_api_key(self) -> None:
        """没有 api_key 时报错。"""
        p = TavilyProvider(api_key="", timeout=30, user_agent="Test")
        with pytest.raises(ValueError, match="API key"):
            _run(p.search("test", 5))

    def test_has_api_url(self) -> None:
        """有 API URL。"""
        assert "api.tavily.com" in TavilyProvider._API_URL


class TestSearxngProvider:
    """SearxngProvider。"""

    def test_requires_base_url(self) -> None:
        """没有 base_url 时报错。"""
        p = SearxngProvider(base_url="", timeout=30, user_agent="Test")
        with pytest.raises(ValueError, match="base_url"):
            _run(p.search("test", 5))

    def test_strips_trailing_slash(self) -> None:
        """去除末尾斜杠。"""
        p = SearxngProvider(base_url="http://localhost:8080/")
        assert p._base_url == "http://localhost:8080"


class TestWebSearchToolConfig:
    """WebSearchTool 配置。"""

    def test_create_with_brave(self, tmp_path: Path) -> None:
        """配置 brave provider 时创建正确。"""
        ctx = _make_ctx(str(tmp_path), provider="brave", api_key="key123")
        tool = WebSearchTool.create(ctx)
        assert tool.provider_name == "brave"
        assert tool.api_key == "key123"

    def test_create_with_searxng(self, tmp_path: Path) -> None:
        """配置 searxng provider 时创建正确。"""
        ctx = _make_ctx(str(tmp_path), provider="searxng", base_url="http://localhost:8080")
        tool = WebSearchTool.create(ctx)
        assert tool.provider_name == "searxng"
        assert tool.base_url == "http://localhost:8080"

    def test_get_provider_brave(self, tmp_path: Path) -> None:
        """_get_provider 返回 BraveProvider。"""
        ctx = _make_ctx(str(tmp_path), provider="brave", api_key="key123")
        tool = WebSearchTool.create(ctx)
        p = tool._get_provider()
        assert isinstance(p, BraveProvider)

    def test_get_provider_searxng(self, tmp_path: Path) -> None:
        """_get_provider 返回 SearxngProvider。"""
        ctx = _make_ctx(str(tmp_path), provider="searxng", base_url="http://localhost:8080")
        tool = WebSearchTool.create(ctx)
        p = tool._get_provider()
        assert isinstance(p, SearxngProvider)
