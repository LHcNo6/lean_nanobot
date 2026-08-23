"""step123: Subagent announce 模板化与 origin_message_id 透传测试（对齐 nanobot）。

验证 step123 的两项改动：
1. ``_announce`` 用 ``subagent_announce.md`` 模板渲染（不再内联 f-string）；
2. ``origin_message_id`` 从 spawn 透传到 announce 元数据（对齐 nanobot）。

全部测试使用 mock bus / 假 runner，禁止真实网络与 API 调用。
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from step123.config.schema import Config
from step123.subagent import SubagentManager


class _CapturingBus:
    """记录 publish_inbound 的轻量假 bus（仅供 _announce 测试）。"""

    def __init__(self) -> None:
        self.sent: list = []

    async def publish_inbound(self, msg) -> None:
        self.sent.append(msg)


class TestAnnounceTemplateRendering(unittest.IsolatedAsyncioTestCase):
    """契约 F1：announce 内容由模板渲染（头部 / Task / Result / Summarize）。"""

    async def test_announce_renders_template(self) -> None:
        """ok 状态：模板产出 [Subagent 'LABEL' completed successfully] + Task + Result。"""
        bus = _CapturingBus()
        mgr = SubagentManager(bus=bus, config=Config())
        await mgr._announce(
            "t1", "MyLabel", "Do the thing", "The result body",
            {"channel": "cli", "chat_id": "c1", "session_key": "s1"}, "ok",
        )
        self.assertTrue(bus.sent)
        content = bus.sent[0].content
        self.assertIn("[Subagent 'MyLabel' completed successfully]", content)
        self.assertIn("Task: Do the thing", content)
        self.assertIn("Result:\nThe result body", content)
        self.assertIn("Summarize this naturally", content)
        # 旧内联 f-string 残留不应出现
        self.assertNotIn("[Subagent 'MyLabel' completed]", content)

    async def test_announce_error_status_text(self) -> None:
        """error 状态：status_text 为 'failed'。"""
        bus = _CapturingBus()
        mgr = SubagentManager(bus=bus, config=Config())
        await mgr._announce(
            "t2", "L2", "task2", "boom",
            {"channel": "cli", "chat_id": "c2", "session_key": "s2"}, "error",
        )
        self.assertIn("[Subagent 'L2' failed]", bus.sent[0].content)


class TestAnnounceOriginMessageId(unittest.IsolatedAsyncioTestCase):
    """契约 F2：origin_message_id 透传到 announce 元数据。"""

    async def test_origin_message_id_threaded(self) -> None:
        """origin 带 origin_message_id 时写入 metadata。"""
        bus = _CapturingBus()
        mgr = SubagentManager(bus=bus, config=Config())
        await mgr._announce(
            "t3", "L3", "task3", "res3",
            {"channel": "cli", "chat_id": "c3", "session_key": "s3",
             "origin_message_id": "msg-xyz"}, "ok",
        )
        meta = bus.sent[0].metadata
        self.assertEqual(meta.get("origin_message_id"), "msg-xyz")
        self.assertEqual(meta.get("injected_event"), "subagent_result")

    async def test_missing_origin_message_id_absent(self) -> None:
        """origin 无 origin_message_id 时不写入该元数据键。"""
        bus = _CapturingBus()
        mgr = SubagentManager(bus=bus, config=Config())
        await mgr._announce(
            "t4", "L4", "task4", "res4",
            {"channel": "cli", "chat_id": "c4"}, "ok",
        )
        self.assertNotIn("origin_message_id", bus.sent[0].metadata)


class TestSpawnThreadsOriginMessageId(unittest.IsolatedAsyncioTestCase):
    """契约 F2（端到端）：spawn 的 origin 经 _run_subagent 透传到 announce。"""

    async def _run_with_fake_runner(self, mgr, origin) -> list:
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
        return [m for m in mgr.bus.sent
                if getattr(m, "metadata", {}).get("injected_event") == "subagent_result"]

    async def test_spawn_propagates_origin_message_id(self) -> None:
        """spawn origin 含 origin_message_id 时，announce 元数据携带它。"""
        bus = _CapturingBus()
        mgr = SubagentManager(bus=bus, config=Config(), workspace=".")
        mgr._provider = object()
        origin = {
            "channel": "cli", "chat_id": "c", "session_key": "s",
            "message_id": "m", "origin_message_id": "om-9",
            "workspace_scope": None,
        }
        announces = await self._run_with_fake_runner(mgr, origin)
        self.assertTrue(announces, "no subagent_result announce captured")
        self.assertEqual(announces[0].metadata.get("origin_message_id"), "om-9")


if __name__ == "__main__":
    unittest.main()
