"""模型运行时解析器（对齐 nanobot `agent/model_runtime.py`）。

将模型选择 / 切换逻辑从 AgentLoop 中提取为独立服务。
step64 为最小增量版：支持 model 切换、preset 选择、context window 调整；
provider 热刷新（refresh）和预设表解析留待 step64 配置层。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from step68.llm import LLMRuntime


class ModelRuntimeResolver:
    """持有不可变 LLMRuntime，提供模型选择与切换能力。

    与 nanobot 的差异：
    - 暂无 model_presets 表 / provider_snapshot_loader（step64 补齐）
    - refresh 为 no-op（无配置热刷新来源）
    - select_preset 仅设置 model_preset 字段，不重建 provider
    """

    def __init__(self, initial_runtime: LLMRuntime) -> None:
        """初始化解析器。

        Args:
            initial_runtime: 初始不可变运行时设置。
        """
        self._runtime: LLMRuntime = initial_runtime

    @property
    def runtime(self) -> LLMRuntime:
        """返回当前不可变运行时（不刷新配置）。"""
        return self._runtime

    @property
    def model_preset(self) -> str | None:
        """当前选中的模型预设名（None 表示使用 provider 默认）。"""
        return self._runtime.model_preset

    @property
    def provider_signature(self) -> tuple[object, ...] | None:
        """当前 provider 快照签名（step64 透传 runtime.snapshot_signature）。"""
        return self._runtime.snapshot_signature

    def current(self, *, refresh: bool = False) -> LLMRuntime:
        """返回选中的运行时，可选刷新默认来源。

        Args:
            refresh: 是否刷新配置（step64 为 no-op，step64 补齐）。

        Returns:
            当前 LLMRuntime。
        """
        # step64：无 provider_snapshot_loader，refresh 暂不实现
        return self._runtime

    def select_model(self, model: str) -> LLMRuntime:
        """切换默认模型（不重建下游 consumer）。

        Args:
            model: 新模型名，非空字符串。

        Returns:
            切换后的 LLMRuntime。

        Raises:
            ValueError: model 为空或非字符串。
        """
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be a non-empty string")
        self._runtime = replace(
            self._runtime,
            model=model.strip(),
            model_preset=None,
        )
        return self._runtime

    def select_preset(self, name: str | None) -> LLMRuntime:
        """选择命名预设为默认（step64 简化版：仅设置 model_preset 字段）。

        nanobot 中此方法会通过 preset_snapshot_loader 重建 provider；
        learn_nano 暂无预设配置表，仅标记 preset 名。

        Args:
            name: 预设名，None 表示清除预设。

        Returns:
            切换后的 LLMRuntime。
        """
        self._runtime = replace(
            self._runtime,
            model_preset=name,
        )
        return self._runtime

    def select_context_window(self, context_window_tokens: int) -> LLMRuntime:
        """切换默认上下文窗口大小。

        Args:
            context_window_tokens: 新的上下文窗口 token 数。

        Returns:
            切换后的 LLMRuntime。

        Raises:
            TypeError: context_window_tokens 非整数。
        """
        if not isinstance(context_window_tokens, int) or isinstance(
            context_window_tokens, bool
        ):
            raise TypeError("context_window_tokens must be an integer")
        self._runtime = replace(
            self._runtime,
            context_window_tokens=context_window_tokens,
        )
        return self._runtime
