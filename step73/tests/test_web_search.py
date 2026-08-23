"""step72：WebSearchTool 单元测试。

使用 mock 模拟 urllib.request.urlopen，避免真实网络请求。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from step73.context import ToolContext
from step73.loader import ToolLoader
from step73.tool import ToolRegistry, ToolResult
from step73.tools.web import (
    DuckDuckGoProvider,
    WebSearchTool,
    _format_search_results,
)


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _make_config(*, web_enable: bool = True, provider: str = "duckduckgo", max_results: int = 5) -> SimpleNamespace:
    return SimpleNamespace(
        web=SimpleNamespace(
            enable=web_enable,
            timeout=30,
            user_agent="TestAgent",
            search=SimpleNamespace(provider=provider, max_results=max_results, timeout=30),
        ),
        tools=SimpleNamespace(restrict_to_workspace=False),
        exec=SimpleNamespace(enable=True, timeout=60, sandbox="", allowed_env_keys=[], allow_patterns=[], deny_patterns=[], path_prepend="", path_append=""),
    )


def _make_ctx(workspace: str, **kwargs) -> ToolContext:
    return ToolContext(
        config=_make_config(**kwargs),
        workspace=workspace,
        restrict_to_workspace=False,
        session_key="test-session",
    )


def _run(coro):
    return asyncio.run(coro)


def _mock_ddg_html(results: list[tuple[str, str, str]]) -> str:
    """生成模拟的 DuckDuckGo HTML 搜索结果页面。

    Args:
        results: [(title, real_url, snippet), ...]

    Returns:
        HTML 字符串。
    """
    from urllib.parse import quote

    blocks = []
    for title, url, snippet in results:
        encoded_url = quote(url, safe="")
        ddg_url = f"https://duckduckgo.com/l/?uddg={encoded_url}&rut=..."
        blocks.append(
            f'<div class="result">'
            f'<a class="result__a" href="{ddg_url}">{title}</a>'
            f'<a class="result__snippet">{snippet}</a>'
            f'</div>'
        )
    return f"<html><body>{''.join(blocks)}</body></html>"


def _mock_response(html: str, status: int = 200):
    """创建模拟的 urllib 响应。"""
    response = MagicMock()
    response.getcode.return_value = status
    response.read.return_value = html.encode("utf-8")
    response.headers = {"Content-Type": "text/html; charset=utf-8"}
    response.__enter__ = MagicMock(return_value=response)
    response.__exit__ = MagicMock(return_value=False)
    return response


# ---------------------------------------------------------------------------
# DuckDuckGoProvider 测试
# ---------------------------------------------------------------------------


class TestDuckDuckGoProvider:
    """DuckDuckGoProvider 测试。"""

    def test_parse_single_result(self) -> None:
        """解析单个搜索结果。"""
        provider = DuckDuckGoProvider()
        html = _mock_ddg_html([
            ("Example Title", "https://example.com", "Example snippet text"),
        ])
        results = provider._parse_results(html, 5)

        assert len(results) == 1
        assert results[0]["title"] == "Example Title"
        assert results[0]["url"] == "https://example.com"
        assert results[0]["content"] == "Example snippet text"

    def test_parse_multiple_results(self) -> None:
        """解析多个搜索结果。"""
        provider = DuckDuckGoProvider()
        html = _mock_ddg_html([
            ("Title 1", "https://one.com", "Snippet 1"),
            ("Title 2", "https://two.com", "Snippet 2"),
            ("Title 3", "https://three.com", "Snippet 3"),
        ])
        results = provider._parse_results(html, 5)

        assert len(results) == 3
        assert results[0]["title"] == "Title 1"
        assert results[2]["url"] == "https://three.com"

    def test_parse_limit_results(self) -> None:
        """限制返回结果数。"""
        provider = DuckDuckGoProvider()
        html = _mock_ddg_html([
            (f"Title {i}", f"https://{i}.com", f"Snippet {i}")
            for i in range(10)
        ])
        results = provider._parse_results(html, 3)

        assert len(results) == 3

    def test_parse_empty_results(self) -> None:
        """无结果时返回空列表。"""
        provider = DuckDuckGoProvider()
        html = "<html><body>No results</body></html>"
        results = provider._parse_results(html, 5)

        assert results == []

    def test_extract_real_url(self) -> None:
        """从 DuckDuckGo 重定向 URL 提取真实 URL。"""
        from urllib.parse import quote

        real = "https://example.com/path?q=test"
        encoded = quote(real, safe="")
        ddg_url = f"https://duckduckgo.com/l/?uddg={encoded}&rut=..."

        result = DuckDuckGoProvider._extract_real_url(ddg_url)
        assert result == real

    def test_extract_real_url_direct(self) -> None:
        """非重定向 URL 直接返回。"""
        url = "https://example.com"
        result = DuckDuckGoProvider._extract_real_url(url)
        assert result == url

    def test_search_with_mock(self) -> None:
        """完整搜索流程（mock HTTP）。"""
        provider = DuckDuckGoProvider()
        html = _mock_ddg_html([
            ("Test Result", "https://test.com", "Test snippet"),
        ])
        mock_resp = _mock_response(html)

        with patch("step72.tools.web.urllib.request.urlopen", return_value=mock_resp):
            results = _run(provider.search("test query", 5))

        assert len(results) == 1
        assert results[0]["title"] == "Test Result"


# ---------------------------------------------------------------------------
# 格式化测试
# ---------------------------------------------------------------------------


class TestFormatSearchResults:
    """搜索结果格式化测试。"""

    def test_format_single(self) -> None:
        """格式化单个结果。"""
        items = [{"title": "Title", "url": "https://x.com", "content": "Snippet"}]
        text = _format_search_results("query", items, 5)

        assert "Results for: query" in text
        assert "1. Title" in text
        assert "https://x.com" in text
        assert "Snippet" in text

    def test_format_multiple(self) -> None:
        """格式化多个结果（编号）。"""
        items = [
            {"title": "A", "url": "https://a.com", "content": "SA"},
            {"title": "B", "url": "https://b.com", "content": "SB"},
        ]
        text = _format_search_results("q", items, 5)

        assert "1. A" in text
        assert "2. B" in text

    def test_format_empty(self) -> None:
        """无结果时返回提示。"""
        text = _format_search_results("test", [], 5)
        assert "No results for: test" in text

    def test_format_limit(self) -> None:
        """限制结果数。"""
        items = [{"title": f"T{i}", "url": f"https://{i}.com", "content": ""} for i in range(10)]
        text = _format_search_results("q", items, 3)

        assert "1. T0" in text
        assert "3. T2" in text
        assert "4." not in text


# ---------------------------------------------------------------------------
# WebSearchTool 测试
# ---------------------------------------------------------------------------


class TestWebSearchTool:
    """WebSearchTool 功能测试。"""

    def test_search_success(self, tmp_path: Path) -> None:
        """搜索成功返回格式化结果。"""
        ctx = _make_ctx(str(tmp_path))
        tool = WebSearchTool.create(ctx)

        html = _mock_ddg_html([
            ("Result 1", "https://one.com", "Snippet 1"),
            ("Result 2", "https://two.com", "Snippet 2"),
        ])
        mock_resp = _mock_response(html)

        with patch("step72.tools.web.urllib.request.urlopen", return_value=mock_resp):
            result = _run(tool.execute(query="test query"))

        text = str(result)
        assert "Results for: test query" in text
        assert "Result 1" in text
        assert "https://one.com" in text

    def test_search_no_results(self, tmp_path: Path) -> None:
        """无结果时返回提示。"""
        ctx = _make_ctx(str(tmp_path))
        tool = WebSearchTool.create(ctx)

        mock_resp = _mock_response("<html><body>No results</body></html>")

        with patch("step72.tools.web.urllib.request.urlopen", return_value=mock_resp):
            result = _run(tool.execute(query="xyzzy_nonexistent"))

        assert "No results for" in str(result)

    def test_search_missing_query(self, tmp_path: Path) -> None:
        """缺少 query 返回错误。"""
        ctx = _make_ctx(str(tmp_path))
        tool = WebSearchTool.create(ctx)

        result = _run(tool.execute(query=None))
        assert isinstance(result, ToolResult)
        assert result.is_error

    def test_search_count_limit(self, tmp_path: Path) -> None:
        """count 参数限制结果数。"""
        ctx = _make_ctx(str(tmp_path))
        tool = WebSearchTool.create(ctx)

        html = _mock_ddg_html([
            (f"R{i}", f"https://{i}.com", f"S{i}") for i in range(10)
        ])
        mock_resp = _mock_response(html)

        with patch("step72.tools.web.urllib.request.urlopen", return_value=mock_resp):
            result = _run(tool.execute(query="test", count=3))

        text = str(result)
        assert "1. R0" in text
        assert "3. R2" in text
        assert "4." not in text

    def test_unsupported_provider(self, tmp_path: Path) -> None:
        """不支持的 provider 返回错误。"""
        ctx = _make_ctx(str(tmp_path), provider="unknown_provider")
        tool = WebSearchTool.create(ctx)

        result = _run(tool.execute(query="test"))
        assert isinstance(result, ToolResult)
        assert result.is_error
        assert "Unsupported" in str(result) or "provider" in str(result).lower()


# ---------------------------------------------------------------------------
# 工具发现与配置
# ---------------------------------------------------------------------------


class TestWebSearchDiscovery:
    """工具发现与配置测试。"""

    def test_tool_discovered(self, tmp_path: Path) -> None:
        """ToolLoader 自动发现 web_search。"""
        ctx = _make_ctx(str(tmp_path))
        registry = ToolRegistry()
        loaded = ToolLoader().load(ctx, registry, scope="core")

        assert "web_search" in loaded
        assert registry.has("web_search")

    def test_both_web_tools_discovered(self, tmp_path: Path) -> None:
        """web_fetch 和 web_search 都被发现。"""
        ctx = _make_ctx(str(tmp_path))
        registry = ToolRegistry()
        loaded = ToolLoader().load(ctx, registry, scope="core")

        assert "web_fetch" in loaded
        assert "web_search" in loaded

    def test_tool_schema(self, tmp_path: Path) -> None:
        """参数 schema 正确。"""
        ctx = _make_ctx(str(tmp_path))
        tool = WebSearchTool.create(ctx)
        schema = tool.to_schema()

        assert schema["function"]["name"] == "web_search"
        props = schema["function"]["parameters"]["properties"]
        assert "query" in props
        assert "count" in props
        assert "query" in schema["function"]["parameters"]["required"]

    def test_tool_read_only(self, tmp_path: Path) -> None:
        """web_search 是只读工具。"""
        ctx = _make_ctx(str(tmp_path))
        tool = WebSearchTool.create(ctx)
        assert tool.read_only is True

    def test_config_disabled(self, tmp_path: Path) -> None:
        """config.web.enable=False 时不加载。"""
        ctx = _make_ctx(str(tmp_path), web_enable=False)
        registry = ToolRegistry()
        loaded = ToolLoader().load(ctx, registry, scope="core")

        assert "web_search" not in loaded

    def test_config_provider_used(self, tmp_path: Path) -> None:
        """配置中的 provider 被使用。"""
        ctx = _make_ctx(str(tmp_path), provider="duckduckgo")
        tool = WebSearchTool.create(ctx)
        assert tool.provider_name == "duckduckgo"

    def test_config_max_results_used(self, tmp_path: Path) -> None:
        """配置中的 max_results 被使用。"""
        ctx = _make_ctx(str(tmp_path), max_results=3)
        tool = WebSearchTool.create(ctx)
        assert tool.max_results == 3
