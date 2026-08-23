"""图片生成工具：ImageGenerationTool（step79）。

对齐 nanobot `agent/tools/image_generation.py` 的最小子集：
- 可插拔 provider 抽象（ImageGenerationProvider ABC）；
- 默认 SimpleSvgProvider 生成占位 SVG（无外部依赖）；
- 支持 prompt/reference_images/aspect_ratio/image_size/count 参数；
- 保存到 workspace/generated 目录。

简化版：不实现真实的 AI 图片生成，不实现 artifact 系统。
"""

from __future__ import annotations

import time
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from step86.schema import ArraySchema, IntegerSchema, StringSchema, tool_parameters_schema
from step86.tool import Tool, ToolResult, tool_parameters


# ---------------------------------------------------------------------------
# Provider 抽象
# ---------------------------------------------------------------------------


class ImageGenerationProvider(ABC):
    """图片生成 provider 抽象基类。"""

    @abstractmethod
    async def generate(
        self,
        prompt: str,
        aspect_ratio: str = "1:1",
        image_size: str = "1K",
    ) -> bytes:
        """生成图片。

        Args:
            prompt: 图片描述。
            aspect_ratio: 宽高比。
            image_size: 尺寸提示。

        Returns:
            图片二进制数据。
        """
        raise NotImplementedError


class SimpleSvgProvider(ImageGenerationProvider):
    """简单 SVG 占位 provider。

    生成一个包含 prompt 文本的彩色 SVG 占位图，无外部依赖。
    """

    # 宽高比 -> 尺寸映射
    _ASPECT_RATIOS = {
        "1:1": (512, 512),
        "16:9": (512, 288),
        "9:16": (288, 512),
        "4:3": (512, 384),
        "3:4": (384, 512),
    }

    async def generate(
        self,
        prompt: str,
        aspect_ratio: str = "1:1",
        image_size: str = "1K",
    ) -> bytes:
        """生成 SVG 占位图。"""
        width, height = self._ASPECT_RATIOS.get(aspect_ratio, (512, 512))

        # 根据 prompt 生成一个颜色
        color_hash = hash(prompt) % 0xFFFFFF
        bg_color = f"#{color_hash:06x}"
        text_color = "#ffffff"

        # 截断 prompt 文本
        display_text = prompt[:60].replace("<", "&lt;").replace(">", "&gt;").replace("&", "&amp;")

        svg = f"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
  <rect width="100%" height="100%" fill="{bg_color}"/>
  <text x="50%" y="50%" dominant-baseline="middle" text-anchor="middle"
        fill="{text_color}" font-family="sans-serif" font-size="14"
        font-weight="bold">{display_text}</text>
  <text x="50%" y="85%" dominant-baseline="middle" text-anchor="middle"
        fill="{text_color}" font-family="sans-serif" font-size="10">
    {aspect_ratio} | {image_size}
  </text>
</svg>"""
        return svg.encode("utf-8")


def _create_provider(name: str) -> ImageGenerationProvider:
    """根据名称创建 provider。

    Args:
        name: provider 名称。

    Returns:
        ImageGenerationProvider 实例。
    """
    providers = {
        "simple": SimpleSvgProvider,
    }
    cls = providers.get(name, SimpleSvgProvider)
    return cls()


# ---------------------------------------------------------------------------
# ImageGenerationTool
# ---------------------------------------------------------------------------


@tool_parameters(tool_parameters_schema(
    prompt=StringSchema("Detailed image generation prompt. Include style, subject, composition, and colors."),
    reference_images=ArraySchema(
        StringSchema("Local path of a reference image for editing."),
        description="Optional reference image paths for iterative edits.",
    ),
    aspect_ratio=StringSchema("Output aspect ratio: 1:1, 16:9, 9:16, 4:3, 3:4."),
    image_size=StringSchema("Output size hint: 1K, 2K, 4K, or 1024x1024."),
    count=IntegerSchema("Number of images to generate (1-8).", minimum=1, maximum=8),
    required=["prompt"],
))
class ImageGenerationTool(Tool):
    """图片生成工具：通过配置的 provider 生成图片并保存到本地。"""

    _scopes = {"core"}
    config_key = "image_generation"

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        """是否启用：读取 ``config.image_generation.enabled``。"""
        return getattr(getattr(ctx.config, "image_generation", None), "enabled", False)

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        """从上下文创建工具实例。"""
        config = getattr(ctx.config, "image_generation", None)
        provider_name = getattr(config, "provider", "simple") if config else "simple"
        save_dir = getattr(config, "save_dir", "generated") if config else "generated"
        return cls(
            workspace=getattr(ctx, "workspace", ""),
            provider=_create_provider(provider_name),
            save_dir=save_dir,
        )

    def __init__(
        self,
        workspace: str = "",
        provider: ImageGenerationProvider | None = None,
        save_dir: str = "generated",
    ):
        """初始化 ImageGenerationTool。

        Args:
            workspace: workspace 路径。
            provider: 图片生成 provider。
            save_dir: 保存目录（相对 workspace）。
        """
        self._workspace = workspace
        self._provider = provider or SimpleSvgProvider()
        self._save_dir = save_dir

    @property
    def name(self) -> str:
        """工具名：``generate_image``。"""
        return "generate_image"

    @property
    def description(self) -> str:
        """工具描述。"""
        return (
            "Generate images from a text prompt. "
            "Supports aspect ratio, size, and multiple images. "
            "Images are saved to the workspace generated/ directory."
        )

    @property
    def read_only(self) -> bool:
        """图片生成工具不是只读（会创建文件）。"""
        return False

    async def execute(
        self,
        prompt: str = "",
        reference_images: list[str] | None = None,
        aspect_ratio: str = "1:1",
        image_size: str = "1K",
        count: int = 1,
        **kwargs: Any,
    ) -> str | ToolResult:
        """生成图片。

        Args:
            prompt: 图片描述。
            reference_images: 参考图片路径。
            aspect_ratio: 宽高比。
            image_size: 尺寸提示。
            count: 生成数量。
            **kwargs: 忽略的额外参数。

        Returns:
            成功时返回图片路径列表文本；失败时返回 ``ToolResult.error``。
        """
        if not prompt or not prompt.strip():
            return ToolResult.error("Error: 'prompt' is required and cannot be empty.")

        if count < 1 or count > 8:
            return ToolResult.error("Error: 'count' must be between 1 and 8.")

        # 创建保存目录
        save_path = Path(self._workspace) / self._save_dir if self._workspace else Path(self._save_dir)
        save_path.mkdir(parents=True, exist_ok=True)

        timestamp = int(time.time())
        generated_paths = []

        for i in range(count):
            try:
                image_data = await self._provider.generate(
                    prompt=prompt,
                    aspect_ratio=aspect_ratio,
                    image_size=image_size,
                )
            except Exception as exc:
                return ToolResult.error(f"Error: Image generation failed: {exc}")

            # 保存文件
            file_id = uuid.uuid4().hex[:8]
            filename = f"img_{timestamp}_{i + 1}_{file_id}.svg"
            filepath = save_path / filename
            filepath.write_bytes(image_data)
            generated_paths.append(filepath.as_posix())

        # 构建结果
        lines = [f"Generated {len(generated_paths)} image(s):"]
        for p in generated_paths:
            lines.append(f"  {p}")

        if reference_images:
            lines.append(f"\nReference images: {', '.join(reference_images)}")

        return "\n".join(lines)
