"""step71：WebFetchTool 单元测试。

使用 mock 模拟 urllib.request.urlopen，避免真实网络请求。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from step123.context import ToolContext
from step123.loader import ToolLoader
from step123.tool import ToolRegistry, ToolResult
from step123.tools.web import WebFetchTool, _normalize, _strip_tags, _validate_url


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _make_config(*, web_enable: bool = True, web_timeout: int = 30) -> SimpleNamespace:
    return SimpleNamespace(
        web=SimpleNamespace(enable=web_enable, timeout=web_timeout, user_agent="TestAgent"),
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


def _mock_response(html: str, status: int = 200, content_type: str = "text/html; charset=utf-8"):
    """创建模拟的 urllib 响应。"""
    response = MagicMock()
    response.getcode.return_value = status
    response.read.return_value = html.encode("utf-8")
    response.headers = {"Content-Type": content_type}
    response.__enter__ = MagicMock(return_value=response)
    response.__exit__ = MagicMock(return_value=False)
    return response


# ---------------------------------------------------------------------------
# 辅助函数测试
# ---------------------------------------------------------------------------


class TestHelpers:
    """辅助函数测试。"""

    def test_validate_url_http(self) -> None:
        """http URL 有效。"""
        valid, msg = _validate_url("http://example.com")
        assert valid is True
        assert msg == ""

    def test_validate_url_https(self) -> None:
        """https URL 有效。"""
        valid, msg = _validate_url("https://example.com/path")
        assert valid is True

    def test_validate_url_ftp_rejected(self) -> None:
        """ftp URL 被拒绝。"""
        valid, msg = _validate_url("ftp://example.com/file")
        assert valid is False
        assert "http" in msg.lower()

    def test_validate_url_file_rejected(self) -> None:
        """file URL 被拒绝。"""
        valid, msg = _validate_url("file:///etc/passwd")
        assert valid is False

    def test_validate_url_missing_domain(self) -> None:
        """缺少域名被拒绝。"""
        valid, msg = _validate_url("http://")
        assert valid is False
        assert "domain" in msg.lower()

    def test_strip_tags_removes_html(self) -> None:
        """去除 HTML 标签。"""
        result = _strip_tags("<h1>Title</h1><p>Content</p>")
        assert result == "TitleContent"

    def test_strip_tags_removes_script(self) -> None:
        """去除 script 内容。"""
        html_text = "<p>Visible</p><script>alert('xss')</script>"
        result = _strip_tags(html_text)
        assert "Visible" in result
        assert "alert" not in result

    def test_strip_tags_removes_style(self) -> None:
        """去除 style 内容。"""
        html_text = "<p>Text</p><style>body { color: red; }</style>"
        result = _strip_tags(html_text)
        assert "Text" in result
        assert "color" not in result

    def test_strip_tags_decodes_entities(self) -> None:
        """解码 HTML 实体。"""
        result = _strip_tags("<p>A &amp; B &lt; C</p>")
        assert "A & B < C" in result

    def test_normalize_whitespace(self) -> None:
        """规范化空白。"""
        result = _normalize("  hello   world  \n\n\n\nfoo")
        assert result == "hello world\n\nfoo"


# ---------------------------------------------------------------------------
# WebFetchTool 测试
# ---------------------------------------------------------------------------


class TestWebFetchTool:
    """WebFetchTool 功能测试。"""

    def test_fetch_simple_html(self, tmp_path: Path) -> None:
        """抓取简单 HTML 页面。"""
        ctx = _make_ctx(str(tmp_path))
        tool = WebFetchTool.create(ctx)

        html_content = "<html><head><title>Test</title></head><body><h1>Hello</h1><p>World</p></body></html>"
        mock_resp = _mock_response(html_content)

        with patch("step71.tools.web.urllib.request.urlopen", return_value=mock_resp):
            result = _run(tool.execute(url="https://example.com"))

        data = json.loads(str(result))
        assert data["status"] == 200
        assert "Hello" in data["content"]
        assert "World" in data["content"]
        assert "<h1>" not in data["content"]  # 标签被去除
        assert data["truncated"] is False

    def test_fetch_includes_banner(self, tmp_path: Path) -> None:
        """输出包含外部内容横幅。"""
        ctx = _make_ctx(str(tmp_path))
        tool = WebFetchTool.create(ctx)

        mock_resp = _mock_response("<p>Test</p>")

        with patch("step71.tools.web.urllib.request.urlopen", return_value=mock_resp):
            result = _run(tool.execute(url="https://example.com"))

        data = json.loads(str(result))
        assert "External content" in data["content"]

    def test_fetch_truncation(self, tmp_path: Path) -> None:
        """长内容被截断。"""
        ctx = _make_ctx(str(tmp_path))
        tool = WebFetchTool.create(ctx)

        long_html = "<p>" + "x" * 100000 + "</p>"
        mock_resp = _mock_response(long_html)

        with patch("step71.tools.web.urllib.request.urlopen", return_value=mock_resp):
            result = _run(tool.execute(url="https://example.com", max_chars=1000))

        data = json.loads(str(result))
        assert data["truncated"] is True
        assert data["length"] <= 1000

    def test_fetch_invalid_url(self, tmp_path: Path) -> None:
        """无效 URL 返回错误。"""
        ctx = _make_ctx(str(tmp_path))
        tool = WebFetchTool.create(ctx)

        result = _run(tool.execute(url="ftp://example.com"))
        assert isinstance(result, ToolResult)
        assert result.is_error
        assert "Invalid URL" in str(result)

    def test_fetch_missing_url(self, tmp_path: Path) -> None:
        """缺少 url 参数返回错误。"""
        ctx = _make_ctx(str(tmp_path))
        tool = WebFetchTool.create(ctx)

        result = _run(tool.execute(url=None))
        assert isinstance(result, ToolResult)
        assert result.is_error

    def test_fetch_url_stripped(self, tmp_path: Path) -> None:
        """URL 首尾空白和引号被去除。"""
        ctx = _make_ctx(str(tmp_path))
        tool = WebFetchTool.create(ctx)

        mock_resp = _mock_response("<p>Test</p>")

        with patch("step71.tools.web.urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
            _run(tool.execute(url='  "https://example.com"  '))
            # 验证请求的 URL 是清洗后的（Request 对象的 full_url）
            call_args = mock_urlopen.call_args
            request_obj = call_args[0][0]
            assert "example.com" in request_obj.full_url
            assert request_obj.full_url.startswith("https://")
            assert '"' not in request_obj.full_url

    def test_fetch_script_removed(self, tmp_path: Path) -> None:
        """script 内容被去除。"""
        ctx = _make_ctx(str(tmp_path))
        tool = WebFetchTool.create(ctx)

        html_content = "<p>Visible</p><script>var secret = 'password';</script>"
        mock_resp = _mock_response(html_content)

        with patch("step71.tools.web.urllib.request.urlopen", return_value=mock_resp):
            result = _run(tool.execute(url="https://example.com"))

        data = json.loads(str(result))
        assert "Visible" in data["content"]
        assert "password" not in data["content"]

    def test_fetch_entities_decoded(self, tmp_path: Path) -> None:
        """HTML 实体被解码。"""
        ctx = _make_ctx(str(tmp_path))
        tool = WebFetchTool.create(ctx)

        html_content = "<p>A &amp; B &lt; C &gt; D</p>"
        mock_resp = _mock_response(html_content)

        with patch("step71.tools.web.urllib.request.urlopen", return_value=mock_resp):
            result = _run(tool.execute(url="https://example.com"))

        data = json.loads(str(result))
        assert "A & B < C > D" in data["content"]


# ---------------------------------------------------------------------------
# 工具发现与配置
# ---------------------------------------------------------------------------


class TestWebFetchDiscovery:
    """工具发现与配置测试。"""

    def test_tool_discovered(self, tmp_path: Path) -> None:
        """ToolLoader 自动发现 web_fetch。"""
        ctx = _make_ctx(str(tmp_path))
        registry = ToolRegistry()
        loaded = ToolLoader().load(ctx, registry, scope="core")

        assert "web_fetch" in loaded
        assert registry.has("web_fetch")

    def test_tool_schema(self, tmp_path: Path) -> None:
        """参数 schema 正确。"""
        ctx = _make_ctx(str(tmp_path))
        tool = WebFetchTool.create(ctx)
        schema = tool.to_schema()

        assert schema["function"]["name"] == "web_fetch"
        props = schema["function"]["parameters"]["properties"]
        assert "url" in props
        assert "max_chars" in props
        assert "url" in schema["function"]["parameters"]["required"]

    def test_tool_read_only(self, tmp_path: Path) -> None:
        """web_fetch 是只读工具。"""
        ctx = _make_ctx(str(tmp_path))
        tool = WebFetchTool.create(ctx)
        assert tool.read_only is True

    def test_config_disabled(self, tmp_path: Path) -> None:
        """config.web.enable=False 时不加载。"""
        ctx = _make_ctx(str(tmp_path), web_enable=False)
        registry = ToolRegistry()
        loaded = ToolLoader().load(ctx, registry, scope="core")

        assert "web_fetch" not in loaded
        assert not registry.has("web_fetch")

    def test_config_timeout_used(self, tmp_path: Path) -> None:
        """配置中的 timeout 被使用。"""
        ctx = _make_ctx(str(tmp_path), web_timeout=15)
        tool = WebFetchTool.create(ctx)
        assert tool.timeout == 15

    def test_enabled_classmethod(self, tmp_path: Path) -> None:
        """enabled 类方法正确读取配置。"""
        ctx_enabled = _make_ctx(str(tmp_path), web_enable=True)
        ctx_disabled = _make_ctx(str(tmp_path), web_enable=False)

        assert WebFetchTool.enabled(ctx_enabled) is True
        assert WebFetchTool.enabled(ctx_disabled) is False
