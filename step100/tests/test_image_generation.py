"""step79：ImageGenerationTool 单元测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from step100.context import ToolContext
from step100.loader import ToolLoader
from step100.tool import ToolRegistry, ToolResult
from step100.tools.image_generation import ImageGenerationTool


def _make_config(*, img_enabled: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        exec=SimpleNamespace(enable=True, timeout=60, sandbox="", allowed_env_keys=[], allow_patterns=[], deny_patterns=[], path_prepend="", path_append=""),
        tools=SimpleNamespace(restrict_to_workspace=False, file=SimpleNamespace(enable=True)),
        web=SimpleNamespace(enable=True, timeout=30, user_agent="Test", search=SimpleNamespace(provider="duckduckgo", max_results=5, timeout=30)),
        my=SimpleNamespace(enable=True, allow_set=False),
        image_generation=SimpleNamespace(enabled=img_enabled, provider="simple", save_dir="generated"),
    )


def _make_ctx(workspace: str, **kwargs) -> ToolContext:
    from step100.tools.file_state import FileStateStore
    from step100.tools.cron import _CronStore
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


class TestImageGeneration:
    """图片生成。"""

    def test_generate_single(self, tmp_path: Path) -> None:
        """生成单张图片。"""
        ctx = _make_ctx(str(tmp_path))
        tool = ImageGenerationTool.create(ctx)

        result = _run(tool.execute(prompt="a cute cat"))

        assert "Generated 1 image(s)" in str(result)
        # 验证文件已创建
        gen_dir = tmp_path / "generated"
        assert gen_dir.exists()
        svg_files = list(gen_dir.glob("*.svg"))
        assert len(svg_files) == 1
        # 验证 SVG 内容
        content = svg_files[0].read_text()
        assert "<svg" in content
        assert "a cute cat" in content

    def test_generate_multiple(self, tmp_path: Path) -> None:
        """生成多张图片。"""
        ctx = _make_ctx(str(tmp_path))
        tool = ImageGenerationTool.create(ctx)

        result = _run(tool.execute(prompt="landscape", count=3))

        assert "Generated 3 image(s)" in str(result)
        gen_dir = tmp_path / "generated"
        svg_files = list(gen_dir.glob("*.svg"))
        assert len(svg_files) == 3

    def test_aspect_ratio(self, tmp_path: Path) -> None:
        """aspect_ratio 影响 SVG 尺寸。"""
        ctx = _make_ctx(str(tmp_path))
        tool = ImageGenerationTool.create(ctx)

        _run(tool.execute(prompt="test", aspect_ratio="16:9"))

        gen_dir = tmp_path / "generated"
        svg_file = list(gen_dir.glob("*.svg"))[0]
        content = svg_file.read_text()
        assert 'width="512"' in content
        assert 'height="288"' in content

    def test_missing_prompt(self, tmp_path: Path) -> None:
        """缺少 prompt 报错。"""
        ctx = _make_ctx(str(tmp_path))
        tool = ImageGenerationTool.create(ctx)

        result = _run(tool.execute(prompt=""))
        assert isinstance(result, ToolResult)
        assert result.is_error

    def test_count_out_of_range(self, tmp_path: Path) -> None:
        """count 超出范围报错。"""
        ctx = _make_ctx(str(tmp_path))
        tool = ImageGenerationTool.create(ctx)

        result = _run(tool.execute(prompt="test", count=10))
        assert isinstance(result, ToolResult)
        assert result.is_error

    def test_with_reference_images(self, tmp_path: Path) -> None:
        """带参考图片。"""
        ctx = _make_ctx(str(tmp_path))
        tool = ImageGenerationTool.create(ctx)

        result = _run(tool.execute(prompt="edit", reference_images=["ref1.png", "ref2.png"]))
        assert "Reference images" in str(result)
        assert "ref1.png" in str(result)


class TestImageGenerationDiscovery:
    """工具发现。"""

    def test_discovered(self, tmp_path: Path) -> None:
        """ToolLoader 自动发现 generate_image。"""
        ctx = _make_ctx(str(tmp_path))
        registry = ToolRegistry()
        loaded = ToolLoader().load(ctx, registry, scope="core")

        assert "generate_image" in loaded
        assert registry.has("generate_image")

    def test_disabled_not_loaded(self, tmp_path: Path) -> None:
        """enabled=False 时不加载。"""
        ctx = _make_ctx(str(tmp_path), img_enabled=False)
        registry = ToolRegistry()
        loaded = ToolLoader().load(ctx, registry, scope="core")

        assert "generate_image" not in loaded

    def test_schema(self, tmp_path: Path) -> None:
        """参数 schema 正确。"""
        ctx = _make_ctx(str(tmp_path))
        tool = ImageGenerationTool.create(ctx)
        schema = tool.to_schema()

        assert schema["function"]["name"] == "generate_image"
        props = schema["function"]["parameters"]["properties"]
        assert "prompt" in props
        assert "aspect_ratio" in props
        assert "count" in props
        assert "reference_images" in props
