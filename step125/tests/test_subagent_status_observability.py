"""step125: self/my 工具可观测子代理状态测试。

验证 `SubagentManager.get_task_statuses()` 与 `MyTool` 的 `subagents` 只读 key，
使父代理可经 `my get subagents` 查询运行中的子代理，对齐 nanobot self.py。

全部测试使用构造数据 / 假管理器，禁止真实网络与 API 调用。
"""

from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from typing import Any

from step125.bus import MessageBus
from step125.config.schema import Config
from step125.subagent import SubagentManager, SubagentStatus
from step125.tools.self import MyTool


def _make_manager_with_status() -> SubagentManager:
    """构造注入一条子代理状态的 SubagentManager（不启动真实 runner）。"""
    mgr = SubagentManager(bus=MessageBus(), config=Config(), workspace=".")
    mgr._task_statuses["t1"] = SubagentStatus(
        task_id="t1",
        label="Research",
        task_description="find papers",
        phase="running",
        iteration=3,
    )
    return mgr


class TestSubagentManagerStatusSnapshot(unittest.TestCase):
    """D1：get_task_statuses 返回可序列化状态列表。"""

    def test_returns_asdict_list(self) -> None:
        mgr = _make_manager_with_status()
        statuses = mgr.get_task_statuses()
        self.assertEqual(len(statuses), 1)
        sta = statuses[0]
        self.assertIsInstance(sta, dict)
        self.assertEqual(sta["task_id"], "t1")
        self.assertEqual(sta["phase"], "running")
        self.assertEqual(sta["iteration"], 3)
        self.assertEqual(sta["label"], "Research")

    def test_empty_when_no_tasks(self) -> None:
        mgr = SubagentManager(bus=MessageBus(), config=Config(), workspace=".")
        self.assertEqual(mgr.get_task_statuses(), [])


class TestMyToolSubagentsKey(unittest.IsolatedAsyncioTestCase):
    """D2/D3：my get subagents 可观测；set 被拒。"""

    async def _get_subagents(self, mgr) -> Any:
        ctx = SimpleNamespace(config=Config(), subagent_manager=mgr)
        tool = MyTool.create(ctx)
        return await tool.execute(action="get", key="subagents")

    async def test_get_subagents_returns_statuses(self) -> None:
        """有子代理时返回含 task_id/phase 的 JSON 列表。"""
        out = await self._get_subagents(_make_manager_with_status())
        data = json.loads(out)
        value = data["value"]
        self.assertIsInstance(value, list)
        self.assertEqual(len(value), 1)
        self.assertEqual(value[0]["task_id"], "t1")
        self.assertEqual(value[0]["phase"], "running")

    async def test_get_subagents_none_manager_returns_empty(self) -> None:
        """无 subagent_manager 时返回空列表，不报错。"""
        out = await self._get_subagents(None)
        data = json.loads(out)
        self.assertEqual(data["value"], [])

    async def test_set_subagents_rejected(self) -> None:
        """set subagents 被 read-only 安全边界拒绝（开启 allow_set 以越过禁用闸门）。"""
        config = SimpleNamespace(my=SimpleNamespace(enable=True, allow_set=True))
        ctx = SimpleNamespace(config=config, subagent_manager=_make_manager_with_status())
        tool = MyTool.create(ctx)
        result = await tool.execute(action="set", key="subagents", value="x")
        # ToolResult.error 的字符串表示含 Error；结构化错误也含 "read-only"
        text = result if isinstance(result, str) else str(result)
        self.assertIn("read-only", text)


if __name__ == "__main__":
    unittest.main()
