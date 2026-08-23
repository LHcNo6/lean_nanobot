"""step125: spawn temperature 覆写测试（G7）。

验证：
1. `LLMRuntime.with_generation_overrides` 覆写 temperature/max_tokens 且原对象不变。
2. `SubagentManager.spawn(temperature=...)` 把覆写写入 origin runtime，
   `_run_subagent` 衍生出的 `spec.temperature` 为覆写值（model/provider 不变）。
3. `temperature=None` 时无覆写（与 step123 行为一致）。
4. `SpawnTool` schema 声明含 `temperature` 参数（0.0–2.0 约束）。

全部测试使用 mock provider / 构造数据，禁止真实网络与 API 调用。
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from step125.bus import MessageBus
from step125.config.schema import Config
from step125.llm import GenerationSettings, LLMRuntime
from step125.subagent import SubagentManager
from step125.tools.spawn import SpawnTool


class TestLLMRuntimeOverrides(unittest.TestCase):
    """F1：with_generation_overrides 行为。"""

    def _base(self) -> LLMRuntime:
        return LLMRuntime(
            provider=object(),
            model="m1",
            generation=GenerationSettings(temperature=0.7, max_tokens=4096, reasoning_effort="low"),
            context_window_tokens=8192,
        )

    def test_temperature_override(self) -> None:
        """覆写 temperature 后新实例生效，原实例不变。"""
        base = self._base()
        overridden = base.with_generation_overrides(temperature=0.3)
        self.assertEqual(overridden.temperature, 0.3)
        self.assertEqual(overridden.max_tokens, 4096)  # 未覆写项保持
        self.assertEqual(overridden.model, "m1")
        self.assertIs(overridden.provider, base.provider)
        self.assertEqual(base.temperature, 0.7)  # 原对象不变

    def test_max_tokens_override_keeps_temperature(self) -> None:
        """覆写 max_tokens 时 temperature 保持。"""
        base = self._base()
        overridden = base.with_generation_overrides(max_tokens=1024)
        self.assertEqual(overridden.max_tokens, 1024)
        self.assertEqual(overridden.temperature, 0.7)


class TestSpawnTemperatureOverride(unittest.IsolatedAsyncioTestCase):
    """F2/F3：spawn 经 origin runtime 覆写 temperature。"""

    def _make_manager(self) -> SubagentManager:
        mgr = SubagentManager(bus=MessageBus(), config=Config(), workspace=".")
        mgr._provider = object()
        return mgr

    def _make_runtime(self, temperature: float = 0.7, model: str = "m1") -> LLMRuntime:
        return LLMRuntime(
            provider=object(),
            model=model,
            generation=GenerationSettings(temperature=temperature, max_tokens=4096),
            context_window_tokens=8192,
        )

    async def _run_with_fake_runner(self, mgr, origin, temperature) -> object:
        captured: dict[str, object] = {}

        async def fake_run(spec):
            captured["spec"] = spec
            return SimpleNamespace(final_content="done", stop_reason="stop")

        mgr.runner.run = fake_run
        await mgr.spawn(task="t", origin=origin, temperature=temperature)
        for _ in range(200):
            if mgr.get_running_count() == 0:
                break
            await asyncio.sleep(0.01)
        return captured["spec"]

    async def test_spawn_applies_temperature_override(self) -> None:
        """契约 B：spawn(temperature=0.3) 使 spec.temperature 被覆写，model/provider 不变。"""
        mgr = self._make_manager()
        origin = {
            "channel": "cli", "chat_id": "c", "session_key": "s",
            "message_id": "m", "workspace_scope": None,
            "runtime": self._make_runtime(temperature=0.7, model="m1"),
        }
        spec = await self._run_with_fake_runner(mgr, origin, 0.3)
        self.assertEqual(spec.temperature, 0.3)
        self.assertEqual(spec.model, "m1")  # model 仍继承 runtime
        self.assertIs(spec.provider, mgr._provider)  # provider 不变

    async def test_spawn_no_override_keeps_runtime_temperature(self) -> None:
        """temperature=None 时 spec.temperature 保持 runtime 原值（无回归）。"""
        mgr = self._make_manager()
        origin = {
            "channel": "cli", "chat_id": "c", "session_key": "s",
            "message_id": "m", "workspace_scope": None,
            "runtime": self._make_runtime(temperature=0.7, model="m1"),
        }
        spec = await self._run_with_fake_runner(mgr, origin, None)
        self.assertEqual(spec.temperature, 0.7)
        self.assertEqual(spec.model, "m1")


class TestSpawnToolDeclaresTemperature(unittest.TestCase):
    """F3：SpawnTool schema 暴露 temperature 参数。"""

    def test_schema_has_temperature(self) -> None:
        params = SpawnTool().parameters
        self.assertIn("temperature", params["properties"])
        temp = params["properties"]["temperature"]
        self.assertEqual(temp["type"], "number")
        self.assertEqual(temp["minimum"], 0.0)
        self.assertEqual(temp["maximum"], 2.0)


if __name__ == "__main__":
    unittest.main()
