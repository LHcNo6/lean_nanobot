"""step116: 子代理 system prompt 模板化测试。

验证：

1. ``_render_subagent_system_prompt`` 始终含 base + ``# Workspace`` 段；
   skills_summary 为空时省略 ``# Skills`` 段，非空时注入。
2. ``SubagentManager._build_subagent_system_prompt`` 在含技能目录的工作区下，
   输出含 workspace 路径与 ``# Skills`` + 技能名；无技能时省略该段。
3. 从原始 ``config`` 提取的 ``disabled_skills`` 能正确排除技能摘要（含
   ``_extract_disabled_skills`` 的多种配置形态回退）。

全部测试使用构造数据 / 临时目录，禁止真实网络与 API 调用。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from step116.bus import MessageBus
from step116.config.schema import Config
from step116.subagent import (
    SubagentManager,
    _extract_disabled_skills,
    _render_subagent_system_prompt,
)


def _make_skill(tmp: Path, name: str, description: str) -> None:
    """在临时工作区的 skills/<name> 下写一个最小 SKILL.md。"""
    skill_dir = tmp / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\nbody\n",
        encoding="utf-8",
    )


class TestRenderSubagentPrompt(unittest.TestCase):
    """模块级渲染函数行为。"""

    def test_render_always_includes_workspace(self) -> None:
        """base + # Workspace 段必含；空 skills 时省略 # Skills。"""
        out = _render_subagent_system_prompt("/ws/root", "")
        self.assertIn("You are a subagent spawned by the main agent.", out)
        self.assertIn("# Workspace", out)
        self.assertIn("/ws/root", out)
        self.assertNotIn("# Skills", out)

    def test_render_includes_skills_section_when_summary_present(self) -> None:
        """非空 skills_summary 时注入 # Skills 段。"""
        summary = "- **demo** — a demo skill  `/ws/root/skills/demo`"
        out = _render_subagent_system_prompt("/ws/root", summary)
        self.assertIn("# Skills", out)
        self.assertIn(summary, out)


class TestManagerPrompt(unittest.IsolatedAsyncioTestCase):
    """SubagentManager._build_subagent_system_prompt 端到端（含技能目录）。"""

    def test_prompt_includes_workspace_and_skills(self) -> None:
        """含技能目录的工作区：prompt 含 workspace 路径与 # Skills + demo。"""
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            _make_skill(tmp, "demo", "demo skill for testing")
            manager = SubagentManager(
                bus=MessageBus(), config=Config(), workspace=str(tmp)
            )
            out = manager._build_subagent_system_prompt(str(tmp))
        self.assertIn("# Workspace", out)
        self.assertIn(str(tmp), out)
        self.assertIn("# Skills", out)
        self.assertIn("demo", out)

    def test_prompt_includes_builtin_skills(self) -> None:
        """无工作区技能目录时，内置技能（如 memory）仍注入 # Skills 段。"""
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            manager = SubagentManager(
                bus=MessageBus(), config=Config(), workspace=str(tmp)
            )
            out = manager._build_subagent_system_prompt(str(tmp))
        self.assertIn("# Workspace", out)
        self.assertIn("# Skills", out)
        self.assertIn("memory", out)

    def test_disabled_skills_excluded_from_summary(self) -> None:
        """disabled_skills 配置生效：指定的技能被排除出摘要，内置技能保留。"""
        config = Config()
        config.agents.defaults.disabled_skills = ["demo"]
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            _make_skill(tmp, "demo", "demo skill for testing")
            manager = SubagentManager(
                bus=MessageBus(), config=config, workspace=str(tmp)
            )
            out = manager._build_subagent_system_prompt(str(tmp))
        self.assertNotIn("demo", out)
        self.assertIn("# Skills", out)
        self.assertIn("memory", out)


class TestExtractDisabledSkills(unittest.TestCase):
    """_extract_disabled_skills 多种配置形态回退。"""

    def test_from_full_config(self) -> None:
        """完整 Config：读取 agents.defaults.disabled_skills。"""
        config = Config()
        config.agents.defaults.disabled_skills = ["a", "b"]
        self.assertEqual(_extract_disabled_skills(config), {"a", "b"})

    def test_from_none_returns_empty(self) -> None:
        """None 配置回退为空集合。"""
        self.assertEqual(_extract_disabled_skills(None), set())

    def test_from_flat_view_returns_empty(self) -> None:
        """已扁平 duck-view（无 agents 段）回退为空集合，不抛错。"""
        flat = SimpleNamespace()
        self.assertEqual(_extract_disabled_skills(flat), set())


if __name__ == "__main__":
    unittest.main()
