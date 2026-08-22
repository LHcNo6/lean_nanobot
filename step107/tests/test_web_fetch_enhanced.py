"""step80：WebFetch 增强单元测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

from step107.context import ToolContext
from step107.tool import ToolResult
from step107.tools.web import WebFetchTool, _extract_readability


def _make_config() -> SimpleNamespace:
    return SimpleNamespace(
        exec=SimpleNamespace(enable=True, timeout=60, sandbox="", allowed_env_keys=[], allow_patterns=[], deny_patterns=[], path_prepend="", path_append=""),
        tools=SimpleNamespace(restrict_to_workspace=False, file=SimpleNamespace(enable=True)),
        web=SimpleNamespace(enable=True, timeout=30, user_agent="Test", search=SimpleNamespace(provider="duckduckgo", max_results=5, timeout=30)),
        my=SimpleNamespace(enable=True, allow_set=False),
        image_generation=SimpleNamespace(enabled=True, provider="simple", save_dir="generated"),
    )


def _make_ctx(workspace: str) -> ToolContext:
    from step107.tools.file_state import FileStateStore
    from step107.tools.cron import _CronStore
    return ToolContext(
        config=_make_config(),
        workspace=workspace,
        restrict_to_workspace=False,
        session_key="test-session",
        file_state_store=FileStateStore(),
        cron_store=_CronStore(),
    )


def _run(coro):
    return asyncio.run(coro)


class TestReadabilityExtraction:
    """readability 正文提取。"""

    def test_removes_script(self) -> None:
        """去除 script 标签内容。"""
        html = "<html><body><p>正文</p><script>alert('xss')</script></body></html>"
        result = _extract_readability(html)
        assert "正文" in result
        assert "alert" not in result
        assert "xss" not in result

    def test_removes_style(self) -> None:
        """去除 style 标签内容。"""
        html = "<html><head><style>body { color: red; }</style></head><body><p>正文</p></body></html>"
        result = _extract_readability(html)
        assert "正文" in result
        assert "color" not in result
        assert "red" not in result

    def test_removes_nav(self) -> None:
        """去除 nav 标签内容。"""
        html = "<nav>首页 关于 联系</nav><main><p>正文内容</p></main>"
        result = _extract_readability(html)
        assert "正文内容" in result
        assert "首页" not in result

    def test_removes_footer(self) -> None:
        """去除 footer 标签内容。"""
        html = "<article><p>文章正文</p></article><footer>版权所有</footer>"
        result = _extract_readability(html)
        assert "文章正文" in result
        assert "版权所有" not in result

    def test_removes_comments(self) -> None:
        """去除 HTML 注释。"""
        html = "<p>正文</p><!-- 这是注释 -->"
        result = _extract_readability(html)
        assert "正文" in result
        assert "这是注释" not in result

    def test_preserves_main_content(self) -> None:
        """保留主要内容。"""
        html = """
        <html>
        <head><title>测试</title></head>
        <body>
        <nav>导航</nav>
        <article>
        <h1>标题</h1>
        <p>这是第一段正文。</p>
        <p>这是第二段正文。</p>
        </article>
        <footer>页脚</footer>
        </body>
        </html>
        """
        result = _extract_readability(html)
        assert "标题" in result
        assert "这是第一段正文" in result
        assert "这是第二段正文" in result
        assert "导航" not in result
        assert "页脚" not in result


class TestWebFetchMode:
    """mode 参数。"""

    def test_invalid_mode(self, tmp_path: Path) -> None:
        """无效 mode 报错。"""
        ctx = _make_ctx(str(tmp_path))
        tool = WebFetchTool.create(ctx)

        result = _run(tool.execute(url="https://example.com", mode="invalid"))
        assert isinstance(result, ToolResult)
        assert result.is_error
        assert "Invalid mode" in str(result)

    def test_default_mode_auto(self, tmp_path: Path) -> None:
        """默认 mode=auto。"""
        ctx = _make_ctx(str(tmp_path))
        tool = WebFetchTool.create(ctx)

        # mock _fetch_sync 返回 HTML
        with patch.object(tool, "_fetch_sync", return_value=(200, "<html><body><p>Hello</p></body></html>")):
            result = _run(tool.execute(url="https://example.com"))
            assert "Hello" in str(result)

    def test_readability_mode(self, tmp_path: Path) -> None:
        """mode=readability 去除噪声。"""
        ctx = _make_ctx(str(tmp_path))
        tool = WebFetchTool.create(ctx)

        html_with_noise = "<html><body><nav>导航</nav><p>正文</p><footer>页脚</footer></body></html>"
        with patch.object(tool, "_fetch_sync", return_value=(200, html_with_noise)):
            result = _run(tool.execute(url="https://example.com", mode="readability"))
            assert "正文" in str(result)
            assert "导航" not in str(result)
            assert "页脚" not in str(result)


class TestWebFetchSchema:
    """参数 schema。"""

    def test_schema_has_mode(self, tmp_path: Path) -> None:
        """schema 包含 mode 参数。"""
        ctx = _make_ctx(str(tmp_path))
        tool = WebFetchTool.create(ctx)
        schema = tool.to_schema()

        props = schema["function"]["parameters"]["properties"]
        assert "mode" in props
        assert "url" in props
        assert "max_chars" in props
