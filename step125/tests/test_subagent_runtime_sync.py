"""step125: Subagent runtime（模型/生成参数）逐父同步测试（对齐 nanobot G5）。

验证 SubagentManager 从 ``origin["runtime"]`` 衍生子代理运行规格：

1. 父会话 runtime.model 继承到 ``spec.model``；
2. 父会话 runtime.temperature 继承到 ``spec.temperature``；
3. 父会话 runtime.max_tokens 继承到 ``spec.max_tokens``；
4. provider 沿用 manager 自身（``self._provider``），生产环境与 ``runtime.provider`` 为同一对象；
5. 无 runtime 时退化为标量缺省（model=None / temperature=0.7 / max_tokens=4096）。

全部测试使用 mock provider / 构造数据，禁止真实网络与 API 调用。
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from step125.bus import MessageBus
from step125.config.schema import Config
from step125.subagent import SubagentManager


class TestSubagentRuntimeSync(unittest.IsolatedAsyncioTestCase):
    """step125（G5）：子代理 AgentRunSpec 继承父会话 runtime 的模型与生成参数。"""

    def _make_manager(self) -> SubagentManager:
        """构造子代理管理器，置非空 provider 越过早退守卫。"""
        mgr = SubagentManager(bus=MessageBus(), config=Config(), workspace=".")
        mgr._provider = object()
        return mgr

    def _make_runtime(self, model: str, temperature: float, max_tokens: int) -> SimpleNamespace:
        """构造一个 runtime-like 对象（仅需 provider/model/temperature/max_tokens）。"""
        return SimpleNamespace(
            provider=object(), model=model, temperature=temperature, max_tokens=max_tokens
        )

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

    async def test_run_spec_inherits_runtime_gen_settings(self) -> None:
        """父会话 runtime 的 model/temperature/max_tokens 继承到子代理 spec。"""
        mgr = self._make_manager()
        runtime = self._make_runtime(model="m1", temperature=0.3, max_tokens=2048)
        origin = {
            "channel": "cli", "chat_id": "c", "session_key": "s",
            "message_id": "m", "workspace_scope": None, "runtime": runtime,
        }
        spec = await self._run_with_fake_runner(mgr, origin)

        # G5-a/b/c：继承父会话模型与生成参数
        self.assertEqual(spec.model, "m1")
        self.assertEqual(spec.temperature, 0.3)
        self.assertEqual(spec.max_tokens, 2048)
        # 契约 F4：provider 沿用 manager 自身（非 runtime.provider）
        self.assertIs(spec.provider, mgr._provider)

    async def test_run_spec_defaults_when_no_runtime(self) -> None:
        """无 runtime 时退化为标量缺省，行为与 step121 一致。"""
        mgr = self._make_manager()
        origin = {
            "channel": "cli", "chat_id": "c", "session_key": "s",
            "message_id": "m", "workspace_scope": None,
        }
        spec = await self._run_with_fake_runner(mgr, origin)

        self.assertIsNone(spec.model)
        self.assertEqual(spec.temperature, 0.7)
        self.assertEqual(spec.max_tokens, 4096)
        self.assertIs(spec.provider, mgr._provider)

    async def test_run_spec_falls_back_to_runtime_provider_when_no_self_provider(self) -> None:
        """self._provider 为 None 时回退 runtime.provider，且不早退。"""
        mgr = SubagentManager(bus=MessageBus(), config=Config(), workspace=".")
        # 故意不设 self._provider（保持 None）
        runtime = self._make_runtime(model="m2", temperature=0.5, max_tokens=1024)
        origin = {
            "channel": "cli", "chat_id": "c", "session_key": "s",
            "message_id": "m", "workspace_scope": None, "runtime": runtime,
        }
        spec = await self._run_with_fake_runner(mgr, origin)

        # 回退到 runtime.provider（非 None）
        self.assertIs(spec.provider, runtime.provider)
        self.assertEqual(spec.model, "m2")
        self.assertEqual(spec.temperature, 0.5)
        self.assertEqual(spec.max_tokens, 1024)


if __name__ == "__main__":
    unittest.main()
