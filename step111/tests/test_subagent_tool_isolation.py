"""step111: Subagent 工具集隔离测试。

验证 SubagentManager._build_tools() 以 scope="subagent" 构建独立裁剪版
注册表（对齐 nanobot ``_build_tools``）：

1. 白名单完整性 —— 恰含 11 个 subagent-scope 工具；
2. 黑名单排除 —— spawn/message/create_goal 等核心专属工具不可见；
3. 端到端防递归 —— 子代理试图调 spawn 时收到"工具不存在"错误结果，
   且 manager 不产生新任务；
4. 组级开关 —— 关闭 web 组后 web_search/web_fetch 缺席；
5. file_state 隔离 —— 每次 _build_tools() 全新 FileStates，跨构建互不共享；
6. 配置扁平化 —— 完整 Config / 已扁平 duck-view / None 三形态统一适配，
   restrict_to_workspace 显式实参优先。

全部测试使用 mock provider / 构造数据，禁止真实网络与 API 调用。
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from step111.bus import MessageBus
from step111.config.schema import Config, ToolsConfig
from step111.llm import LLMResponse, ToolCallRequest
from step111.provider import LLMProvider
from step111.subagent import SubagentManager, _flatten_tools_config


# learn_nano 当前已声明 subagent scope 的工具全集（对齐 api-spec B1）。
SUBAGENT_TOOL_NAMES = {
    "exec",
    "read_file",
    "write_file",
    "edit_file",
    "list_dir",
    "find_files",
    "grep",
    "web_search",
    "web_fetch",
    "write_stdin",
    "apply_patch",
}

# 核心专属工具（scope 不含 subagent），子代理必须不可见（api-spec B2）。
CORE_ONLY_TOOL_NAMES = {
    "spawn",
    "message",
    "create_goal",
    "update_goal",
    "echo",
    "generate_image",
    "glob",
}


class _RecursiveSpawnProvider(LLMProvider):
    """首轮返回名为 spawn 的工具调用，模拟子代理尝试递归 spawn。"""

    def __init__(self) -> None:
        self.calls: list[list[dict]] = []

    @property
    def model(self) -> str:
        """模型名（mock）。"""
        return "mock"

    async def chat(self, messages, tools=None, model=None, temperature=0.7, max_tokens=4096):
        """首轮请求 spawn 工具；次轮读取工具错误结果后直接收尾。"""
        self.calls.append([dict(m) for m in messages])
        if len(self.calls) == 1:
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="call_recursive_spawn",
                        name="spawn",
                        arguments={"task": "nested task", "label": "Nested"},
                    )
                ],
                finish_reason="tool_calls",
                usage={"prompt_tokens": 10, "completion_tokens": 4},
            )
        return LLMResponse(
            content="gave up", finish_reason="stop",
            usage={"prompt_tokens": 12, "completion_tokens": 3},
        )


class TestBuildToolsWhitelist(unittest.IsolatedAsyncioTestCase):
    """_buildTools 白名单/黑名单/开关/隔离行为。"""

    def setUp(self) -> None:
        """每个用例新建 manager（完整 Config 形态）。"""
        self.manager = SubagentManager(
            bus=MessageBus(), config=Config(), workspace="."
        )

    def test_build_tools_contains_exactly_subagent_set(self) -> None:
        """契约 B1：恰含 11 个 subagent-scope 工具，不多不少。"""
        registry = self.manager._build_tools()
        loaded = set(registry._tools.keys())
        self.assertEqual(loaded, SUBAGENT_TOOL_NAMES)

    def test_build_tools_excludes_core_only_tools(self) -> None:
        """契约 B2：核心专属工具在子代理注册表中不可见。"""
        registry = self.manager._build_tools()
        leaked = CORE_ONLY_TOOL_NAMES & set(registry._tools.keys())
        self.assertEqual(leaked, set(), f"core-only tools leaked: {leaked}")

    def test_group_toggle_respected(self) -> None:
        """关闭 web 组后 web_search / web_fetch 应缺席（组级开关生效）。"""
        config = Config()
        config.tools.web.enable = False
        manager = SubagentManager(bus=MessageBus(), config=config)
        loaded = set(manager._build_tools()._tools.keys())
        self.assertNotIn("web_search", loaded)
        self.assertNotIn("web_fetch", loaded)
        # 其余工具不受影响
        self.assertIn("exec", loaded)
        self.assertIn("read_file", loaded)

    def test_exec_toggle_respected(self) -> None:
        """关闭 exec 组后 shell 工具应缺席。"""
        config = Config()
        config.tools.exec.enable = False
        manager = SubagentManager(bus=MessageBus(), config=config)
        loaded = set(manager._build_tools()._tools.keys())
        self.assertNotIn("exec", loaded)

    def test_file_state_isolated_per_spawn(self) -> None:
        """契约 B6：两次构建的 FileStates 互不共享；ExecSessionManager 共享。"""
        r1 = self.manager._build_tools()
        r2 = self.manager._build_tools()
        fs1 = r1.get("read_file")._explicit_file_states
        fs2 = r2.get("read_file")._explicit_file_states
        self.assertIsNotNone(fs1)
        self.assertIsNot(fs1, fs2)
        # exec_session_manager 由 manager 持有并跨构建共享
        sm1 = r1.get("write_stdin")._session_manager
        sm2 = r2.get("write_stdin")._session_manager
        self.assertIs(sm1, sm2)
        self.assertIs(sm1, self.manager._exec_session_manager)


class TestRecursiveSpawnPrevention(unittest.IsolatedAsyncioTestCase):
    """端到端：子代理无法递归 spawn。"""

    async def test_spawned_subagent_cannot_recursive_spawn(self) -> None:
        """契约 B3：spawn 工具不存在 → 错误结果 + 不产生新任务。"""
        bus = MessageBus()
        provider = _RecursiveSpawnProvider()
        manager = SubagentManager(bus=bus, provider=provider)

        await manager.spawn(task="outer task")
        # 等待后台任务结束（announce 完成后计数归零）
        for _ in range(100):
            if manager.get_running_count() == 0 and not bus.inbound.empty():
                break
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.05)

        # 1) 没有任何嵌套任务被创建过（running 计数始终归零）
        self.assertEqual(manager.get_running_count(), 0)
        # 2) 子代理第二轮请求中包含"工具不存在"的错误工具结果
        self.assertEqual(len(provider.calls), 2)
        tool_results = [
            m for m in provider.calls[1]
            if m.get("role") == "tool"
        ]
        self.assertTrue(tool_results, "expected a tool result row in second request")
        joined = "".join(str(m.get("content", "")) for m in tool_results)
        self.assertIn("not found", joined)
        self.assertIn("spawn", joined)


class TestFlattenToolsConfig(unittest.TestCase):
    """配置扁平化三形态 + restrict 显式覆盖。"""

    def test_full_pydantic_config_flattened(self) -> None:
        """完整 Config：web/exec 提升至根级，tools 段保留。"""
        config = Config()
        flat, restrict = _flatten_tools_config(config)
        self.assertIsNotNone(flat.web)
        self.assertIsNotNone(flat.exec)
        self.assertIs(flat.tools, config.tools)
        self.assertFalse(restrict)

    def test_flat_duck_view_passthrough(self) -> None:
        """已扁平视图（测试惯用 SimpleNamespace）：字段级透传且不修改原对象。"""
        tools_sec = SimpleNamespace(restrict_to_workspace=True)
        view = SimpleNamespace(web=SimpleNamespace(enable=True), exec=None, tools=tools_sec)
        flat, restrict = _flatten_tools_config(view)
        self.assertIs(flat.web, view.web)
        self.assertIs(flat.exec, view.exec)
        self.assertIs(flat.tools, tools_sec)
        self.assertTrue(restrict)

    def test_none_uses_default_tools_config(self) -> None:
        """None 输入：从空 ToolsConfig 构造默认扁平视图。"""
        flat, restrict = _flatten_tools_config(None)
        self.assertIsNotNone(flat.web)
        self.assertIsNotNone(flat.exec)
        self.assertIsInstance(flat.tools, ToolsConfig)
        self.assertFalse(restrict)

    def test_restrict_override_wins_in_manager(self) -> None:
        """restrict_to_workspace 显式实参优先于配置值，且同步覆写视图。"""
        config = Config()  # 默认 restrict_to_workspace=False
        manager = SubagentManager(
            bus=MessageBus(), config=config, restrict_to_workspace=True
        )
        self.assertTrue(manager._restrict_to_workspace)
        self.assertTrue(manager._config.tools.restrict_to_workspace)

    def test_restrict_falls_back_to_config(self) -> None:
        """未显式传参时回落 config.tools.restrict_to_workspace。"""
        config = Config()
        config.tools.restrict_to_workspace = True
        manager = SubagentManager(bus=MessageBus(), config=config)
        self.assertTrue(manager._restrict_to_workspace)


if __name__ == "__main__":
    unittest.main()
