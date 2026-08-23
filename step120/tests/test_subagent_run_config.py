"""step120: Subagent 运行配置传播测试（对齐 nanobot 子代理运行配置）。

验证 SubagentManager 把父配置的运行限制传播到子代理 ``AgentRunSpec``：

1. ``agents.defaults.max_tool_result_chars`` → ``spec.governance_config.max_tool_result_chars``；
2. ``agents.defaults.fail_on_tool_error`` → ``spec.fail_on_tool_error``；
3. ``finalize_on_max_iterations`` 硬编码为 ``False``（对齐 nanobot 子代理语义）；
4. ``max_iterations_message`` 硬编码为 nanobot 同款文案。

全部测试使用 mock provider / 构造数据，禁止真实网络与 API 调用。
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from step120.bus import MessageBus
from step120.config.schema import Config
from step120.subagent import SubagentManager


class TestExtractRunConfig(unittest.TestCase):
    """从原始 config 提取运行配置（契约 E1/E2）。"""

    def test_max_tool_result_chars_default(self) -> None:
        """无 config 时回退默认 16_000。"""
        mgr = SubagentManager(bus=MessageBus())
        self.assertEqual(mgr._max_tool_result_chars, 16_000)

    def test_max_tool_result_chars_from_config(self) -> None:
        """Config.agents.defaults.max_tool_result_chars 被提取。"""
        config = Config()
        config.agents.defaults.max_tool_result_chars = 512
        mgr = SubagentManager(bus=MessageBus(), config=config)
        self.assertEqual(mgr._max_tool_result_chars, 512)

    def test_fail_on_tool_error_default(self) -> None:
        """无 config 时回退默认 True。"""
        mgr = SubagentManager(bus=MessageBus())
        self.assertTrue(mgr._fail_on_tool_error)

    def test_fail_on_tool_error_from_config(self) -> None:
        """Config.agents.defaults.fail_on_tool_error=False 被提取。"""
        config = Config()
        config.agents.defaults.fail_on_tool_error = False
        mgr = SubagentManager(bus=MessageBus(), config=config)
        self.assertFalse(mgr._fail_on_tool_error)


class TestSubagentRunConfigPropagation(unittest.IsolatedAsyncioTestCase):
    """step120：子代理 AgentRunSpec 传播运行配置（契约 E1-E4）。"""

    def _make_manager(self) -> SubagentManager:
        """构造子代理管理器，置非空 provider 越过早退守卫。"""
        mgr = SubagentManager(bus=MessageBus(), config=Config(), workspace=".")
        mgr._provider = object()
        return mgr

    async def _run_with_fake_runner(self, mgr, origin) -> object:
        """用记录 spec 的假 runner 替换 manager.runner.run，等待后台任务结束。"""
        captured: dict[str, object] = {}

        async def fake_run(spec):
            captured["spec"] = spec
            return SimpleNamespace(final_content="done", stop_reason="stop")

        mgr.runner.run = fake_run
        await mgr.spawn(task="t", origin=origin)
        for _ in range(200):
            if mgr.get_running_count() == 0:
                break
            await asyncio.sleep(0.01)
        return captured["spec"]

    async def test_spec_propagates_config_and_hardcoded(self) -> None:
        """子代理 spec 携带治理配置、错误策略与 nanobot 一致性硬编码项。"""
        config = Config()
        config.agents.defaults.max_tool_result_chars = 1024
        config.agents.defaults.fail_on_tool_error = False
        mgr = SubagentManager(bus=MessageBus(), config=config, workspace=".")
        mgr._provider = object()
        origin = {
            "channel": "cli", "chat_id": "c", "session_key": "s",
            "message_id": "m", "workspace_scope": None,
        }
        spec = await self._run_with_fake_runner(mgr, origin)

        # E1：治理配置传播 max_tool_result_chars
        self.assertIsNotNone(spec.governance_config)
        self.assertEqual(spec.governance_config.max_tool_result_chars, 1024)
        # E2：工具错误升级策略传播
        self.assertFalse(spec.fail_on_tool_error)
        # E3：max-iterations 由隐形续跑接管（对齐 nanobot 硬编码 False）
        self.assertFalse(spec.finalize_on_max_iterations)
        # E4：收尾文案对齐 nanobot 子代理
        self.assertEqual(
            spec.max_iterations_message,
            "Task completed but no final response was generated.",
        )

    async def test_default_config_uses_defaults(self) -> None:
        """默认 Config（不改写）下子代理 spec 使用默认阈值与 True 错误策略。"""
        mgr = self._make_manager()
        origin = {
            "channel": "cli", "chat_id": "c", "session_key": "s",
            "message_id": "m", "workspace_scope": None,
        }
        spec = await self._run_with_fake_runner(mgr, origin)
        self.assertEqual(spec.governance_config.max_tool_result_chars, 16_000)
        self.assertTrue(spec.fail_on_tool_error)
        self.assertFalse(spec.finalize_on_max_iterations)


if __name__ == "__main__":
    unittest.main()
