"""Step 27 Skills 加载器测试（A11）。

全部使用构造数据（tmp_path 临时目录 + monkeypatch shutil.which / os.environ），
不依赖任何真实环境与 API Key。覆盖：
- ``skills/loader.py``：frontmatter 解析（dict / JSON 字符串 / nanobot+openclaw 键 /
  非法 YAML / 原生类型保留）、frontmatter 剥离（含 CRLF）、list_skills
  （workspace 覆盖内置 / disabled 过滤 / filter_unavailable / 目录缺失）、
  load_skill 优先序、load_skills_for_context 分区拼接、build_skills_summary
  （格式 / unavailable 标注 / exclude / 空）、get_skill_availability /
  get_skill_requirements、get_always_skills（顶层与 metadata 内 always + 不可用排除）；
- ``context.py``：ContextBuilder 注入 —— always 全量进 # Active Skills、
  普通技能只进 # Skills 摘要、disabled_skills 隐藏、skill_names 显式注入、
  session_summary 共存；
- ``loop.py from_config``：agents.defaults.disabled_skills 传入 context_builder。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from step36.bus import MessageBus
from step36.config.schema import Config
from step36.context import ContextBuilder
from step36.loop import AgentLoop
from step36.skills import SkillsLoader
from step36.skills.loader import BUILTIN_SKILLS_DIR, _STRIP_SKILL_FRONTMATTER


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def _write_skill(base: Path, name: str, body: str, frontmatter: str | None = None) -> Path:
    """在 ``base/<name>/SKILL.md`` 写入一个技能文件。"""
    skill_dir = base / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    path = skill_dir / "SKILL.md"
    if frontmatter is not None:
        content = f"---\n{frontmatter}\n---\n\n{body}"
    else:
        content = body
    path.write_text(content, encoding="utf-8")
    return path


def _loader(tmp_path: Path) -> tuple[Path, Path]:
    """构造 (workspace, builtin) 两个临时目录；builtin 预置 weather 与 memory 两个技能。"""
    ws = tmp_path / "ws"
    builtin = tmp_path / "builtin"
    ws.mkdir()
    builtin.mkdir()
    _write_skill(
        builtin, "weather", "WEATHER-BODY",
        frontmatter='name: "weather"\ndescription: "Get weather."\nmetadata: {"nanobot": {"requires": {"bins": ["curl"]}}}\n',
    )
    _write_skill(
        builtin, "memory", "MEMORY-BODY",
        frontmatter='name: "memory"\ndescription: "Memory skill."\nalways: true\n',
    )
    return ws, builtin


def _ws_skill(ws: Path, name: str, body: str, frontmatter: str | None = None) -> Path:
    """向 workspace 根目录写一个技能（loader 从 ``<ws>/skills/`` 读取）。"""
    return _write_skill(ws / "skills", name, body, frontmatter=frontmatter)


def _make_loader(tmp_path: Path, *, disabled: set[str] | None = None) -> SkillsLoader:
    """带默认临时 builtin 目录的加载器，避免依赖仓库内置目录。"""
    ws, builtin = _loader(tmp_path)
    return SkillsLoader(ws, builtin_skills_dir=builtin, disabled_skills=disabled)


# ---------------------------------------------------------------------------
# frontmatter 解析
# ---------------------------------------------------------------------------


class TestFrontmatterParse:
    def test_basic_fields(self, tmp_path):
        loader = _make_loader(tmp_path)
        _ws_skill(tmp_path / "ws", "demo", "b",
            frontmatter='name: "demo"\ndescription: "A demo."\nalways: true\n',
        )
        meta = loader.get_skill_metadata("demo")
        assert meta == {"name": "demo", "description": "A demo.", "always": True}

    def test_metadata_json_string(self, tmp_path):
        loader = _make_loader(tmp_path)
        _ws_skill(tmp_path / "ws", "demo", "b",
            frontmatter='metadata: {"nanobot": {"requires": {"bins": ["gh"]}}}\n',
        )
        assert loader._get_skill_meta("demo") == {"requires": {"bins": ["gh"]}}

    def test_metadata_yaml_dict(self, tmp_path):
        loader = _make_loader(tmp_path)
        _ws_skill(tmp_path / "ws", "demo", "b",
            frontmatter="metadata:\n  nanobot:\n    requires:\n      bins: [git]\n      env: [TOKEN]\n",
        )
        assert loader._get_skill_meta("demo") == {"requires": {"bins": ["git"], "env": ["TOKEN"]}}

    def test_openclaw_fallback(self, tmp_path):
        loader = _make_loader(tmp_path)
        _ws_skill(tmp_path / "ws", "demo", "b",
            frontmatter='metadata: {"openclaw": {"requires": {"bins": ["jq"]}}}\n',
        )
        assert loader._get_skill_meta("demo") == {"requires": {"bins": ["jq"]}}

    def test_nanobot_preferred_over_openclaw(self, tmp_path):
        loader = _make_loader(tmp_path)
        _ws_skill(tmp_path / "ws", "demo", "b",
            frontmatter='metadata: {"nanobot": {"requires": {"bins": ["a"]}}, "openclaw": {"requires": {"bins": ["b"]}}}\n',
        )
        assert loader._get_skill_meta("demo") == {"requires": {"bins": ["a"]}}

    def test_invalid_yaml_returns_none(self, tmp_path):
        loader = _make_loader(tmp_path)
        _ws_skill(tmp_path / "ws", "demo", "b", frontmatter="name: [unclosed\n")
        assert loader.get_skill_metadata("demo") is None

    def test_no_frontmatter_returns_none(self, tmp_path):
        loader = _make_loader(tmp_path)
        _ws_skill(tmp_path / "ws", "demo", "plain body")
        assert loader.get_skill_metadata("demo") is None

    def test_native_types_preserved(self, tmp_path):
        loader = _make_loader(tmp_path)
        _ws_skill(tmp_path / "ws", "demo", "b", frontmatter='count: 5\nflag: true\n')
        meta = loader.get_skill_metadata("demo")
        assert meta == {"count": 5, "flag": True}

    def test_regex_matches_crlf(self):
        match = _STRIP_SKILL_FRONTMATTER.match("---\r\nname: demo\r\nalways: true\r\n---\r\nbody\r\n")
        assert match is not None
        assert "name: demo" in match.group(1)

    def test_strip_frontmatter(self, tmp_path):
        loader = _make_loader(tmp_path)
        _ws_skill(tmp_path / "ws", "demo", "body text", frontmatter='name: "demo"\n')
        assert loader._strip_frontmatter(loader.load_skill("demo")) == "body text"

    def test_strip_frontmatter_no_header(self, tmp_path):
        loader = _make_loader(tmp_path)
        _ws_skill(tmp_path / "ws", "demo", "  plain text  ")
        assert loader._strip_frontmatter(loader.load_skill("demo")) == "  plain text  "


# ---------------------------------------------------------------------------
# list / load
# ---------------------------------------------------------------------------


def _no_builtin(tmp_path: Path) -> str:
    """一个不存在的内置目录（隔离仓库 builtin_skills，保证测试确定性）。"""
    return str(tmp_path / "absent-builtin")


class TestListSkills:
    def test_workspace_only(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        _ws_skill(ws, "one", "b", frontmatter='name: "one"\n')
        loader = SkillsLoader(ws, builtin_skills_dir=tmp_path / "absent")
        names = [e["name"] for e in loader.list_skills(filter_unavailable=False)]
        assert names == ["one"]

    def test_workspace_overrides_builtin(self, tmp_path):
        ws, builtin = _loader(tmp_path)
        _ws_skill(ws, "weather", "WS-WEATHER", frontmatter='name: "weather"\n')
        loader = SkillsLoader(ws, builtin_skills_dir=builtin)
        entries = loader.list_skills(filter_unavailable=False)
        weather = [e for e in entries if e["name"] == "weather"][0]
        assert weather["source"] == "workspace"
        assert "WS-WEATHER" in loader.load_skill("weather")

    def test_disabled_skills_filtered(self, tmp_path):
        loader = _make_loader(tmp_path, disabled={"memory"})
        names = [e["name"] for e in loader.list_skills(filter_unavailable=False)]
        assert "memory" not in names
        # nanobot 语义：disabled 只影响 list_skills（注入路径），
        # 直接 load_skill 仍可读原文（供 read_file 类工具绕过）。

    def test_filter_unavailable_bins(self, tmp_path, monkeypatch):
        loader = _make_loader(tmp_path)
        monkeypatch.setattr("shutil.which", lambda p: None)
        names = [e["name"] for e in loader.list_skills()]
        assert "weather" not in names  # requires curl
        assert "memory" in names  # 无 requires

    def test_filter_unavailable_env(self, tmp_path, monkeypatch):
        loader = _make_loader(tmp_path)
        _ws_skill(tmp_path / "ws", "token-skill", "b",
            frontmatter='metadata: {"nanobot": {"requires": {"env": ["TEST_TOKEN"]}}}\n',
        )
        monkeypatch.delenv("TEST_TOKEN", raising=False)
        assert "token-skill" not in [e["name"] for e in loader.list_skills()]
        monkeypatch.setenv("TEST_TOKEN", "x")
        assert "token-skill" in [e["name"] for e in loader.list_skills()]

    def test_missing_directories(self, tmp_path):
        loader = SkillsLoader(tmp_path / "absent", builtin_skills_dir=tmp_path / "absent2")
        assert loader.list_skills() == []
        assert loader.load_skill("nope") is None

    def test_load_skill_precedence_and_fallback(self, tmp_path):
        ws, builtin = _loader(tmp_path)
        _ws_skill(ws, "weather", "WS-COPY")
        loader = SkillsLoader(ws, builtin_skills_dir=builtin)
        assert loader.load_skill("weather") == "WS-COPY"
        assert "MEMORY-BODY" in loader.load_skill("memory")
        assert loader.load_skill("missing") is None

    def test_list_skill_entries_shape(self, tmp_path):
        loader = _make_loader(tmp_path)
        for entry in loader.list_skills(filter_unavailable=False):
            assert set(entry) == {"name", "path", "source"}
            assert Path(entry["path"]).name == "SKILL.md"


# ---------------------------------------------------------------------------
# 上下文产出
# ---------------------------------------------------------------------------


class TestContextLoading:
    def test_load_skills_for_context_strips_frontmatter(self, tmp_path):
        loader = _make_loader(tmp_path)
        out = loader.load_skills_for_context(["weather", "ghost"])
        assert "### Skill: weather" in out
        assert "WEATHER-BODY" in out
        assert "ghost" not in out
        assert out.split("### Skill: weather")[1].split("---")[0]  # 分区存在
        assert "name:" not in out

    def test_load_skills_for_context_empty(self, tmp_path):
        loader = SkillsLoader(tmp_path, builtin_skills_dir=tmp_path / "absent")
        assert loader.load_skills_for_context(["nope"]) == ""

    def test_build_skills_summary_format_available(self, tmp_path):
        loader = _make_loader(tmp_path)
        summary = loader.build_skills_summary()
        assert "**memory**" in summary
        # memory 无 requires → 无 unavailable 标注
        memory_line = [ln for ln in summary.splitlines() if "**memory**" in ln][0]
        assert "unavailable" not in memory_line
        assert "SKILL.md" in memory_line

    def test_build_skills_summary_unavailable_annotation(self, tmp_path, monkeypatch):
        loader = _make_loader(tmp_path)
        monkeypatch.setattr("shutil.which", lambda p: None)
        summary = loader.build_skills_summary()
        weather_line = [ln for ln in summary.splitlines() if "**weather**" in ln][0]
        assert "unavailable: CLI: curl" in weather_line
        memory_line = [ln for ln in summary.splitlines() if "**memory**" in ln][0]
        assert "unavailable" not in memory_line

    def test_build_skills_summary_exclude(self, tmp_path):
        loader = _make_loader(tmp_path)
        summary = loader.build_skills_summary(exclude={"memory"})
        assert "**memory**" not in summary
        assert "**weather**" in summary

    def test_build_skills_summary_empty(self, tmp_path):
        loader = SkillsLoader(tmp_path, builtin_skills_dir=tmp_path / "absent")
        assert loader.build_skills_summary() == ""

    def test_get_skill_availability(self, tmp_path, monkeypatch):
        loader = _make_loader(tmp_path)
        assert loader.get_skill_availability("memory") == (True, "")
        monkeypatch.setattr("shutil.which", lambda p: None)
        available, reason = loader.get_skill_availability("weather")
        assert available is False
        assert "CLI: curl" in reason

    def test_get_skill_requirements(self, tmp_path, monkeypatch):
        loader = _make_loader(tmp_path)
        monkeypatch.setattr("shutil.which", lambda p: None)
        reqs = loader.get_skill_requirements("weather")
        assert reqs == {
            "bins": ["curl"],
            "env": [],
            "missing_bins": ["curl"],
            "missing_env": [],
        }

    def test_get_always_skills_top_level(self, tmp_path):
        loader = _make_loader(tmp_path)
        assert loader.get_always_skills() == ["memory"]

    def test_get_always_skills_nanobot_metadata(self, tmp_path):
        loader = _make_loader(tmp_path)
        _ws_skill(tmp_path / "ws", "meta-always", "b",
            frontmatter='metadata: {"nanobot": {"always": true}}\n',
        )
        assert "meta-always" in loader.get_always_skills()

    def test_get_always_skills_unavailable_excluded(self, tmp_path, monkeypatch):
        loader = _make_loader(tmp_path)
        _ws_skill(tmp_path / "ws", "always-req", "b",
            frontmatter='always: true\nmetadata: {"nanobot": {"requires": {"bins": ["no-such-tool"]}}}\n',
        )
        monkeypatch.setattr("shutil.which", lambda p: None)
        assert "always-req" not in loader.get_always_skills()


# ---------------------------------------------------------------------------
# ContextBuilder 注入
# ---------------------------------------------------------------------------


class TestContextBuilderInjection:
    def test_always_skill_injected_full(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        _ws_skill(ws, "memory", "MEMORY-BODY", frontmatter='name: "memory"\nalways: true\n')
        ctx = ContextBuilder(workspace=str(ws), builtin_skills_dir=_no_builtin(tmp_path))
        # 注入 memory（always）全量
        prompt = ctx.build_system_prompt()
        assert "# Active Skills" in prompt
        assert "### Skill: memory" in prompt
        assert "MEMORY-BODY" in prompt

    def test_summary_section_created(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        _ws_skill(ws, "normal", "TOOL-BODY", frontmatter='name: "normal"\ndescription: "A tool skill."\n')
        ctx = ContextBuilder(workspace=str(ws), builtin_skills_dir=_no_builtin(tmp_path))
        prompt = ctx.build_system_prompt()
        assert "# Skills" in prompt
        assert "**normal** — A tool skill." in prompt
        assert "read_file" in prompt
        assert "TOOL-BODY" not in prompt  # 非 always 不注入全文

    def test_always_excluded_from_summary(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        _ws_skill(ws, "memory", "MEM", frontmatter='name: "memory"\nalways: true\n')
        ctx = ContextBuilder(workspace=str(ws), builtin_skills_dir=_no_builtin(tmp_path))
        prompt = ctx.build_system_prompt()
        assert "MEM" in prompt
        assert "# Skills" not in prompt  # 只剩 always 技能时无摘要

    def test_disabled_skills_hidden(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        _ws_skill(ws, "memory", "MEM-BODY", frontmatter='name: "memory"\nalways: true\n')
        _ws_skill(ws, "normal", "NORM-BODY", frontmatter='name: "normal"\n')
        ctx = ContextBuilder(workspace=str(ws), builtin_skills_dir=_no_builtin(tmp_path), disabled_skills=["memory", "normal"])
        prompt = ctx.build_system_prompt()
        assert "MEM-BODY" not in prompt
        assert "NORM-BODY" not in prompt
        assert "# Skills" not in prompt

    def test_skill_names_explicit_injection(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        _ws_skill(ws, "normal", "EXPLICIT-BODY", frontmatter='name: "normal"\n')
        ctx = ContextBuilder(workspace=str(ws), builtin_skills_dir=_no_builtin(tmp_path))
        prompt = ctx.build_system_prompt(skill_names=["normal"])
        assert "EXPLICIT-BODY" in prompt
        assert "### Skill: normal" in prompt
        assert "# Skills" not in prompt  # 无其他技能 → 无摘要

    def test_skill_names_still_summarize_others(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        _ws_skill(ws, "explicit", "EX", frontmatter='name: "explicit"\n')
        _ws_skill(ws, "other", "OT", frontmatter='name: "other"\ndescription: "Other skill."\n')
        ctx = ContextBuilder(workspace=str(ws), builtin_skills_dir=_no_builtin(tmp_path))
        prompt = ctx.build_system_prompt(skill_names=["explicit"])
        assert "EX" in prompt
        assert "**other** — Other skill." in prompt  # 其他技能仍进摘要

    def test_session_summary_still_works(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        ctx = ContextBuilder(workspace=str(ws), builtin_skills_dir=_no_builtin(tmp_path))
        prompt = ctx.build_system_prompt(session_summary="User likes Python.")
        assert "[Archived Context Summary]" in prompt
        assert "# Skills" not in prompt

    def test_workspace_without_skills(self, tmp_path):
        ctx = ContextBuilder(workspace=str(tmp_path / "ws"), builtin_skills_dir=_no_builtin(tmp_path))
        prompt = ctx.build_system_prompt()
        assert "# Active Skills" not in prompt
        assert "# Skills" not in prompt


# ---------------------------------------------------------------------------
# Agent.from_config 贯通
# ---------------------------------------------------------------------------


class TestFromConfigWiring:
    def test_disabled_skills_from_defaults(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        config = Config(
            providers={"openai": {"apiKey": "sk-test"}},
            agents={"defaults": {"workspace": str(ws), "disabledSkills": ["memory"]}},
        )
        loop = AgentLoop.from_config(config, bus=MessageBus())
        assert loop.context.disabled_skills == ["memory"]
        names = [s["name"] for s in loop.context.skills.list_skills(filter_unavailable=False)]
        assert "memory" not in names