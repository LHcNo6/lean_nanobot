"""Agent 技能（Skills）加载器。

对齐 nanobot `agent/skills.py` 的最小集（A11）：
- SKILL.md 文件分布在两个来源：
  - workspace：``<workspace>/skills/<name>/SKILL.md``
  - 内置：``<step>/builtin_skills/<name>/SKILL.md``（默认目录，可注入覆盖）
  - **workspace 同名覆盖内置**（同一 name 只保留 workspace 条目）；
- SKILL.md 带 YAML frontmatter：``name`` / ``description`` / ``always``（顶层布尔，
  表示全量注入上下文）/ ``metadata``（YAML dict 或 JSON 字符串，内含
  ``nanobot`` / ``openclaw`` 嵌套键，携带 ``requires: {bins: [...], env: [...]}``
  与 ``always`` 等能力元数据，对齐 openclaw skill 生态）；
- 可用性过滤：``requires.bins`` 用 ``shutil.which`` 检查、``requires.env`` 用
  ``os.environ`` 检查，全部满足才算可用；``disabled_skills`` 直接剔除；
- 渐进加载：``build_skills_summary`` 只给摘要（名称/描述/路径/不可用原因），
  agent 需要时再读完整 SKILL.md；``get_always_skills`` + ``load_skills_for_context``
  用于把标记为 always 的技能全量注入上下文。

与 nanobot 的差异（刻意简化）：
- 不解析 ``metadata.install``（安装指引）、``emoji`` 等展示字段（留待将来）；
- 不加载 skill 的 ``resources/`` 附属文件（只读 SKILL.md 本体）。
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

import yaml

# 默认内置技能目录：step28/builtin_skills（本文件位于 step28/skills/loader.py，
# 上上级即 step28 根目录；不直接命名为 skills 以避免与代码包同名冲突）。
BUILTIN_SKILLS_DIR = Path(__file__).resolve().parent.parent / "builtin_skills"

# 匹配开头的 `---` frontmatter：YAML 体（group 1）+ 独立成行的收尾 `---`；兼容 CRLF。
_STRIP_SKILL_FRONTMATTER = re.compile(
    r"^---\s*\r?\n(.*?)\r?\n---\s*\r?\n?",
    re.DOTALL,
)


class SkillsLoader:
    """Agent 技能加载器。

    职责：发现（workspace + 内置）、元数据解析、可用性过滤、渐进加载摘要与
    全量内容注入。核心产出供 ``ContextBuilder.build_system_prompt`` 消费。
    """

    def __init__(
        self,
        workspace: str | Path,
        builtin_skills_dir: str | Path | None = None,
        disabled_skills: set[str] | None = None,
    ):
        """初始化加载器。

        Args:
            workspace: 工作区根目录（其下 ``skills/`` 放 workspace 技能）。
            builtin_skills_dir: 内置技能目录；缺省用模块级默认值。
            disabled_skills: 需要禁用的技能名集合（优先于一切来源过滤）。
        """
        self.workspace = Path(workspace)
        self.workspace_skills = self.workspace / "skills"
        self.builtin_skills = Path(builtin_skills_dir) if builtin_skills_dir else BUILTIN_SKILLS_DIR
        self.disabled_skills = disabled_skills or set()

    # ------------------------------------------------------------------
    # 技能发现
    # ------------------------------------------------------------------

    def _skill_entries_from_dir(
        self, base: Path, source: str, *, skip_names: set[str] | None = None
    ) -> list[dict[str, str]]:
        """扫描一个目录，收集其下的技能条目。

        Args:
            base: 技能根目录（直接子目录为技能）。
            source: 来源标识（"workspace" / "builtin"）。
            skip_names: 需跳过的技能名（用于 workspace 覆盖内置）。

        Returns:
            技能条目列表：``{"name", "path", "source"}``。
        """
        if not base.exists():
            return []
        entries: list[dict[str, str]] = []
        for skill_dir in base.iterdir():
            if not skill_dir.is_dir():
                continue
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.exists():
                continue
            name = skill_dir.name
            if skip_names is not None and name in skip_names:
                continue
            entries.append({"name": name, "path": str(skill_file), "source": source})
        return entries

    def list_skills(self, filter_unavailable: bool = True) -> list[dict[str, str]]:
        """列出全部可用技能条目（workspace 优先，覆盖同名内置）。

        Args:
            filter_unavailable: True 时过滤掉需求未满足的技能。

        Returns:
            技能条目列表：``{"name", "path", "source"}``。
        """
        skills = self._skill_entries_from_dir(self.workspace_skills, "workspace")
        workspace_names = {entry["name"] for entry in skills}
        if self.builtin_skills and self.builtin_skills.exists():
            skills.extend(
                self._skill_entries_from_dir(self.builtin_skills, "builtin", skip_names=workspace_names)
            )

        if self.disabled_skills:
            skills = [s for s in skills if s["name"] not in self.disabled_skills]

        if filter_unavailable:
            return [skill for skill in skills if self._check_requirements(self._get_skill_meta(skill["name"]))]
        return skills

    def load_skill(self, name: str) -> str | None:
        """按名加载技能（SKILL.md 原文，含 frontmatter）。

        Args:
            name: 技能名（目录名）。

        Returns:
            SKILL.md 内容；不存在返回 None。workspace 优先于内置。
        """
        roots = [self.workspace_skills]
        if self.builtin_skills:
            roots.append(self.builtin_skills)
        for root in roots:
            path = root / name / "SKILL.md"
            if path.exists():
                return path.read_text(encoding="utf-8")
        return None

    # ------------------------------------------------------------------
    # 元数据与要求解析
    # ------------------------------------------------------------------

    def get_skill_metadata(self, name: str) -> dict | None:
        """解析技能 frontmatter 元数据。

        Args:
            name: 技能名。

        Returns:
            元数据 dict（键转 str，值保留 YAML 原生类型）；无 frontmatter /
            非法 YAML 返回 None。
        """
        content = self.load_skill(name)
        if not content or not content.startswith("---"):
            return None
        match = _STRIP_SKILL_FRONTMATTER.match(content)
        if not match:
            return None
        try:
            parsed = yaml.safe_load(match.group(1))
        except yaml.YAMLError:
            return None
        if not isinstance(parsed, dict):
            return None
        metadata: dict[str, object] = {}
        for key, value in parsed.items():
            metadata[str(key)] = value
        return metadata

    def _get_skill_meta(self, name: str) -> dict:
        """取技能 nanobot/openclaw 元数据（``frontmatter.metadata`` 内的嵌套键）。"""
        raw_meta = self.get_skill_metadata(name) or {}
        return self._parse_nanobot_metadata(raw_meta.get("metadata"))

    def _parse_nanobot_metadata(self, raw: object) -> dict:
        """从 frontmatter 的 ``metadata`` 字段提取 nanobot/openclaw 元数据。

        ``raw`` 可以是 YAML dict（yaml.safe_load 已解析）或 JSON 字符串；
        兼容两者并以 ``nanobot`` 键优先、``openclaw`` 兜底。

        Args:
            raw: metadata 字段原始值。

        Returns:
            提取到的元数据 dict；无法解析时返回空 dict。
        """
        if isinstance(raw, dict):
            data = raw
        elif isinstance(raw, str):
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return {}
        else:
            return {}
        if not isinstance(data, dict):
            return {}
        payload = data.get("nanobot", data.get("openclaw", {}))
        return payload if isinstance(payload, dict) else {}

    def _check_requirements(self, skill_meta: dict) -> bool:
        """检查技能需求（bins/env）是否全部满足。

        Args:
            skill_meta: 技能 nanobot 元数据（含 ``requires``）。

        Returns:
            True 表示所有要求的 CLI 命令与环境变量都存在。
        """
        requires = skill_meta.get("requires", {})
        required_bins = requires.get("bins", [])
        required_env_vars = requires.get("env", [])
        return all(shutil.which(cmd) for cmd in required_bins) and all(
            os.environ.get(var) for var in required_env_vars
        )

    def _get_missing_requirements(self, skill_meta: dict) -> str:
        """描述缺失的需求（供摘要标注 "unavailable" 用）。

        Args:
            skill_meta: 技能元数据。

        Returns:
            ``"CLI: xxx, ENV: yyy"`` 风格的缺失描述；无缺失返回空串。
        """
        requires = skill_meta.get("requires", {})
        required_bins = requires.get("bins", [])
        required_env_vars = requires.get("env", [])
        return ", ".join(
            [f"CLI: {command_name}" for command_name in required_bins if not shutil.which(command_name)]
            + [f"ENV: {env_name}" for env_name in required_env_vars if not os.environ.get(env_name)]
        )

    def get_skill_availability(self, name: str) -> tuple[bool, str]:
        """返回技能是否可用及不可用原因。

        Args:
            name: 技能名。

        Returns:
            ``(available, reason)``；可用时 reason 为空串。
        """
        meta = self._get_skill_meta(name)
        available = self._check_requirements(meta)
        return available, "" if available else self._get_missing_requirements(meta)

    def get_skill_requirements(self, name: str) -> dict[str, list[str]]:
        """返回技能声明的命令/环境变量要求与当前缺失项。

        Args:
            name: 技能名。

        Returns:
            ``{"bins", "env", "missing_bins", "missing_env"}``。
        """
        requires = self._get_skill_meta(name).get("requires", {})
        bins = [str(value) for value in requires.get("bins", [])]
        env = [str(value) for value in requires.get("env", [])]
        return {
            "bins": bins,
            "env": env,
            "missing_bins": [value for value in bins if not shutil.which(value)],
            "missing_env": [value for value in env if not os.environ.get(value)],
        }

    # ------------------------------------------------------------------
    # 上下文注入产出
    # ------------------------------------------------------------------

    @staticmethod
    def _strip_frontmatter(content: str) -> str:
        """剥离 markdown 开头的 YAML frontmatter 区块。

        Args:
            content: SKILL.md 全文。

        Returns:
            去头后的正文（首尾 strip）；无 frontmatter 时原样返回。
        """
        if not content.startswith("---"):
            return content
        match = _STRIP_SKILL_FRONTMATTER.match(content)
        if match:
            return content[match.end():].strip()
        return content

    def load_skills_for_context(self, skill_names: list[str]) -> str:
        """加载指定技能全量内容（frontmatter 已剥离），用于上下文注入。

        Args:
            skill_names: 技能名列表（跳过不存在的）。

        Returns:
            `### Skill: <name>` 分区拼接成的 markdown；找不到任何技能返回 ""。
        """
        parts = [
            f"### Skill: {name}\n\n{self._strip_frontmatter(markdown)}"
            for name in skill_names
            if (markdown := self.load_skill(name))
        ]
        return "\n\n---\n\n".join(parts)

    def build_skills_summary(self, exclude: set[str] | None = None) -> str:
        """构建全部技能的摘要（渐进加载入口）。

        只列出 名称 — 描述 + 路径（以及不可用原因），agent 需要时再读全文。

        Args:
            exclude: 需要在摘要中省略的技能名（如已全量注入的 always 技能）。

        Returns:
            markdown 列表；无技能返回 ""。
        """
        all_skills = self.list_skills(filter_unavailable=False)
        if not all_skills:
            return ""

        lines: list[str] = []
        for entry in all_skills:
            skill_name = entry["name"]
            if exclude and skill_name in exclude:
                continue
            meta = self._get_skill_meta(skill_name)
            available = self._check_requirements(meta)
            desc = self._get_skill_description(skill_name)
            if available:
                lines.append(f"- **{skill_name}** — {desc}  `{entry['path']}`")
            else:
                missing = self._get_missing_requirements(meta)
                suffix = f" (unavailable: {missing})" if missing else " (unavailable)"
                lines.append(f"- **{skill_name}** — {desc}{suffix}  `{entry['path']}`")
        return "\n".join(lines)

    def get_always_skills(self) -> list[str]:
        """获取标记为 ``always=true`` 且可用（需求满足）的技能名列表。

        ``always`` 可在 nanobot 元数据（``metadata.nanobot.always``）或
        frontmatter 顶层声明。

        Returns:
            always 技能名列表。
        """
        return [
            entry["name"]
            for entry in self.list_skills(filter_unavailable=True)
            if (meta := self.get_skill_metadata(entry["name"]) or {})
            and (
                self._parse_nanobot_metadata(meta.get("metadata")).get("always")
                or meta.get("always")
            )
        ]

    def _get_skill_description(self, name: str) -> str:
        """取技能描述（frontmatter ``description``）；缺省回退为技能名。"""
        meta = self.get_skill_metadata(name)
        if meta and meta.get("description"):
            return meta["description"]
        return name