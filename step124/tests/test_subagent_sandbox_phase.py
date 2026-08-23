"""step124: 子代理 ToolContext 沙箱 + 多相位状态测试（G9 + G10）。

验证：
1. G9：子代理 `_build_tools` 构造的 `ToolContext` 携带 `workspace_sandbox`
   （`WorkspaceSandboxStatus` 实例，与 `restrict_to_workspace` 一致）。
2. G10：子代理 `SubagentStatus.phase` 随 runner 迭代被 checkpoint_callback 更新为非终态相位
   （`tools_completed` / `awaiting_tools` 等），父代理可观测运行状态。

全部测试使用 mock provider / 构造数据，禁止真实网络与 API 调用。
"""

from __future__ import annotations

import asyncio
import unittest
from typing import Any
from unittest import mock

from step124.bus import MessageBus
from step124.config.schema import Config
from step124.context import ToolContext
from step124.llm import LLMResponse, ToolCallRequest
from step124.provider import LLMProvider
from step124.security.workspace_access import WorkspaceSandboxStatus
from step124.subagent import SubagentManager


class TestSubagentToolContextSandbox(unittest.IsolatedAsyncioTestCase):
    """step124（G9）：子代理 ToolContext 注入 workspace_sandbox。"""

    def test_build_tools_injects_workspace_sandbox(self) -> None:
        """契约 B：_build_tools 构造的 ToolContext.workspace_sandbox 为有效状态。"""
        mgr = SubagentManager(bus=MessageBus(), config=Config(), workspace=".")

        captured: list[dict[str, Any]] = []

        class _SpyToolContext(ToolContext):
            def __init__(self, **kwargs: Any) -> None:
                captured.append(kwargs)
                super().__init__(**kwargs)

        with mock.patch("step124.subagent.ToolContext", _SpyToolContext):
            mgr._build_tools()

        self.assertTrue(captured, "ToolContext 未被构造")
        kwargs = captured[0]
        self.assertIn("workspace_sandbox", kwargs)
        sandbox = kwargs["workspace_sandbox"]
        self.assertIsInstance(sandbox, WorkspaceSandboxStatus)
        # sandbox 的 restrict 意图与 manager 配置一致
        self.assertEqual(sandbox.restrict_to_workspace, mgr._restrict_to_workspace)

    def test_build_tools_sandbox_default_off_when_unrestricted(self) -> None:
        """未限制时 workspace_sandbox.level 为 'off'（与 workspace_sandbox_status 一致）。"""
        mgr = SubagentManager(bus=MessageBus(), config=Config(), workspace=".")
        mgr._restrict_to_workspace = False

        captured: list[dict[str, Any]] = []

        class _SpyToolContext(ToolContext):
            def __init__(self, **kwargs: Any) -> None:
                captured.append(kwargs)
                super().__init__(**kwargs)

        with mock.patch("step124.subagent.ToolContext", _SpyToolContext):
            mgr._build_tools()

        sandbox = captured[0]["workspace_sandbox"]
        self.assertFalse(sandbox.restrict_to_workspace)
        self.assertEqual(sandbox.level, "off")


class _PhaseRecordingProvider(LLMProvider):
    """首轮返回 list_exec_sessions 工具调用，次轮返回终态文本；记录运行期相位。"""

    def __init__(self, mgr: SubagentManager) -> None:
        self.mgr = mgr
        self.calls = 0
        self.phases: list[str] = []

    @property
    def model(self) -> str:
        """模型名（mock）。"""
        return "mock"

    async def chat(self, messages, tools=None, model=None, temperature=0.7, max_tokens=4096):
        """记录当前子代理相位；首轮请求工具，次轮收尾。"""
        self.calls += 1
        statuses = self.mgr.get_task_statuses()
        if statuses:
            self.phases.append(statuses[0]["phase"])
        if self.calls == 1:
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="call_ls", name="list_exec_sessions", arguments={}
                    )
                ],
                finish_reason="tool_calls",
                usage={"prompt_tokens": 1, "completion_tokens": 1},
            )
        return LLMResponse(
            content="done", finish_reason="stop",
            usage={"prompt_tokens": 1, "completion_tokens": 1},
        )


class TestSubagentPhaseCheckpoint(unittest.IsolatedAsyncioTestCase):
    """step124（G10）：checkpoint_callback 驱动 status.phase 多相位。"""

    async def test_status_phase_updates_during_run(self) -> None:
        """契约 C：运行期 status.phase 出现非终态相位（tools_completed）。"""
        mgr = SubagentManager(bus=MessageBus(), config=Config(), workspace=".")
        provider = _PhaseRecordingProvider(mgr)
        mgr._provider = provider

        origin = {
            "channel": "cli", "chat_id": "c", "session_key": "s",
            "message_id": "m", "workspace_scope": None,
        }
        await mgr.spawn(task="t", origin=origin)
        for _ in range(200):
            if mgr.get_running_count() == 0:
                break
            await asyncio.sleep(0.01)

        # 至少经历了非 initial/done/error 的中间相位
        self.assertTrue(
            any(p in ("awaiting_tools", "tools_completed", "final_response") for p in provider.phases),
            f"未观测到多相位更新，记录相位={provider.phases}",
        )
        # 终态为 done
        self.assertEqual(mgr.get_task_statuses(), [])


if __name__ == "__main__":
    unittest.main()
