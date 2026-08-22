"""ReadFileTool 向后兼容模块（step76 已迁移到 filesystem.py）。

本模块仅做 re-export，确保旧代码的导入路径继续工作。
实际实现位于 ``step76.tools.filesystem.ReadFileTool``。
"""

from __future__ import annotations

from step104.tools.filesystem import ReadFileTool

__all__ = ["ReadFileTool"]
