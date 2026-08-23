"""附件处理工具（对齐 nanobot `utils/document.py` 的最小增量子集）。

step64 实现：
- is_image_file：mimetypes 扩展判断图片
- reference_non_image_attachments：分离图片和非图片附件引用

文档文本提取（PDF/docx/xlsx/pptx）和 base64 vision 编码留待后续。
"""

from __future__ import annotations

import mimetypes
from pathlib import Path


def is_image_file(path: str) -> bool:
    """判断路径是否看起来是图片文件。

    step64 简化版：仅用 mimetypes 扩展判断，不读文件头 magic bytes。

    Args:
        path: 文件路径。

    Returns:
        True 如果 MIME 类型以 image/ 开头。
    """
    mime, _ = mimetypes.guess_type(path)
    return bool(mime and mime.startswith("image/"))


def reference_non_image_attachments(
    content: str,
    media: list[str],
) -> tuple[str, list[str]]:
    """分离图片和非图片附件，不读取文件内容。

    图片路径保留在返回的 media 列表中（供下游 vision block 处理）；
    非图片路径以 ``[Attachment: path]`` 引用追加到 content。

    Args:
        content: 原始消息文本。
        media: 附件路径列表。

    Returns:
        (更新后的 content, 图片路径列表)
    """
    image_paths: list[str] = []
    attachment_refs: list[str] = []
    for path in media:
        if is_image_file(path):
            image_paths.append(path)
        else:
            attachment_refs.append(f"[Attachment: {path}]")
    if attachment_refs:
        suffix = "\n".join(attachment_refs)
        content = f"{content}\n\n{suffix}" if content else suffix
    return content, image_paths
