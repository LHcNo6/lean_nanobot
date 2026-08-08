# Step 27 — Skills 加载器（A11）

在 Step 26（事件层）基础上，把 nanobot 的 Skills 体系最小集落地：
SKILL.md frontmatter 解析与可用性过滤（`requires: bins/env`）、workspace 覆盖
内置、`disabled_skills` 过滤、渐进加载（摘要注入 + read_file 读全文）与
always 技能全量注入，ContextBuilder 与 `Agent.from_config` 全链路打通。

---

## 一、这一阶段解决了什么问题、为什么要这样做

Step 26 之前，ContextBuilder 的 system prompt 只会拼接
`AGENTS.md / SOUL.md / USER.md` 三个引导文件：任何超出这些文件的可复用
工作流（查天气、调 GitHub、内容总结……）都只能硬编码进 `AGENTS.md`，或者靠
prompt 里一次性糊一大段指令——不可复用、不可按环境禁用、不可声明依赖。

nanobot 的做法（`nanobot/agent/skills.py` + `context.py`）：
- **技能即 markdown 文件**：`<skills 目录>/<name>/SKILL.md`，头部 YAML
  frontmatter 声明 `name` / `description` / `always` / `metadata`（内含
  `requires: {bins, env}`、`always` 等能力元数据，兼容 openclaw 生态的
  JSON 字符串形态）；
- **两源合并 + 覆盖**：workspace 技能优先，同名覆盖内置技能；`disabled_skills`
  直接剔除；
- **可用性过滤**：`requires.bins` 用 `shutil.which`、`requires.env` 用
  `os.environ` 检查，未安装的 CLI / 缺失的环境变量使技能标注为
  "unavailable"（并给出缺失项，agent 可尝试安装）；
- **渐进加载**：prompt 里只放全部技能的摘要行（`- **name** — desc  \`path\``），
  agent 需要时才用 read_file 读全文 —— 技能多时不会撑爆上下文；
- **always 例外**：`always: true` 的技能（如 memory）直接全量注入
  `# Active Skills` 区块。

本 step 对齐这条链路的完整最小集。
`ContextBuilder.skills` 使用的默认内置目录就是本 step 的
`<repo>/step27/builtin_skills/`，随仓库自带两个演示技能（weather、memory），
可观察 workspace 覆盖、可用性过滤、disabled 与 always 注入的真实行为。

## 二、目标与实现

| 目标 | 实现 |
|------|------|
| 技能发现 | `skills/loader.py:SkillsLoader` —— workspace（`<ws>/skills/`）+ 内置目录两源合并，workspace 同名覆盖内置（`_skill_entries_from_dir(..., skip_names=...)`），`disabled_skills` 集中过滤 |
| frontmatter 解析 | `get_skill_metadata`（`yaml.safe_load`，键转 str、原生类型保留）；`_strip_frontmatter`（正则 `^---...\n---`，兼容 CRLF）；`_parse_nanobot_metadata`（`metadata` 字段支持 dict 或 JSON 字符串，`nanobot` 键优先、`openclaw` 兜底） |
| 可用性过滤 | `_check_requirements`（bins→`shutil.which`，env→`os.environ`，全满足才算可用）；`get_skill_availability` / `get_skill_requirements` 输出缺失项 |
| 渐进加载 | `build_skills_summary(exclude=...)`：全部技能摘要行，不可用技能带 `(unavailable: CLI: xxx / ENV: yyy)` 标注 + SKILL.md 路径，供 agent 按需 read_file |
| always 注入 | `get_always_skills`（可用且 `metadata.nanobot.always` 或顶层 `always`）；`load_skills_for_context` 全量读正文（frontmatter 已剥离），`### Skill: <name>` 分区拼接 |
| ContextBuilder 注入 | `context.py`：新增 `disabled_skills` / `builtin_skills_dir` 字段与惰性 `skills` 属性；`build_system_prompt(..., skill_names=None)` 注入两块：`# Active Skills`（always + 显式指定的 skill_names 全量）+ `# Skills`（其余技能渐进摘要）|
| config 贯通 | `loop.py:from_config` 把 `agents.defaults.disabled_skills`（step25 预留字段）传给 ContextBuilder；`main.py` 同步 |
| 演示技能 | `builtin_skills/weather`（`requires.bins: [curl]`，演示可用性过滤）+ `builtin_skills/memory`（`always: true`，演示全量注入） |
| 测试 | `tests/test_skills.py`（pytest，39 个，全构造数据 / tmp_path / monkeypatch，无真实 API Key、不依赖真实环境）|

## 三、核心函数 / 类说明

### `skills/loader.py`
- `BUILTIN_SKILLS_DIR`：默认内置技能目录（`step27/builtin_skills`，避开与代码
  包同名冲突）。
- `SkillsLoader(workspace, builtin_skills_dir=None, disabled_skills=None)`：
  - `list_skills(filter_unavailable=True)`：合并条目 `{name, path, source}`；
  - `load_skill(name)`：workspace 优先回退内置，返回 SKILL.md 原文；
  - `load_skills_for_context(names)`：剥离 frontmatter 的正文拼接；
  - `build_skills_summary(exclude)`：渐进加载摘要；
  - `get_skill_availability / get_skill_requirements`：可用性与要求明细；
  - `get_always_skills()`：always 且可用的技能名（nanobot 元数据或顶层均认）；
  - `_check_requirements / _get_missing_requirements`：bins/env 双通道检查。
- frontmatter 形态（对齐 openclaw）：

  ```yaml
  ---
  name: weather
  description: Get current weather and forecasts (no API key required).
  always: true                        # 可选：全量注入
  metadata: {"nanobot": {"requires": {"bins": ["curl"], "env": ["KEY"]}}}
  ---
  ```

### `context.py`
- `ContextBuilder`（dataclass）新增字段：`disabled_skills: list[str]`、
  `builtin_skills_dir: str | None`；`skills` 属性惰性构建并缓存 SkillsLoader。
- `build_system_prompt(identity=None, session_summary=None, skill_names=None)`：
  1. always 技能全量进 `# Active Skills`；
  2. 其余技能进 `# Skills` 渐进摘要（canonical 文案对齐 nanobot
     skills_section.md："read its SKILL.md file using the read_file tool"）；
  3. `skill_names` 显式指定的技能也全量注入（lean 扩展 —— nanobot 声明了
     该参数但当前未使用，见"取舍"）。

### `loop.py`
- `from_config`：`ContextBuilder(workspace=..., disabled_skills=list(defaults.disabled_skills))`，config 驱动禁用名单。

## 四、暴露的问题 / 取舍

| 取舍 | 说明 | 后续计划 |
|------|------|----------|
| 内置演示技能使全部 system prompt 变长 | 仓库含 `builtin_skills/` 的 memory（always）会出现在所有默认 workspace 的 prompt 中（回归测试已核实无精确断言，388 unittest 全绿） | 产品侧由用户改用自有内置目录或全局 disabled_skills |
| `disabled_skills` 只过滤 `list_skills` 注入路径 | `load_skill` 直接读仍可拿到原文（对齐 nanobot；read_file 类绕过不在控制内） | — |
| `skill_names` 显式注入是 lean 扩展 | nanobot 声明该参数但未实现；lean 版实现为"全量注入指定技能"并用摘要排除 | step30 子代理系统提示可直接复用 |
| 不解析 `metadata.install` / `emoji` | 只取 `requires.bins/env` 与 `always` | 展示字段留待真实通道/WebUI |
| 不加载 `skills/<name>/resources/` | 只读 SKILL.md 本体 | — |

## 五、下一步要解决什么

Step 28 — Workspace 安全模型 + 运行时上下文（A10 + A9 + H7）：
SkillsLoader 与工具都依赖真实 workspace 权限模型（`WorkspaceScopeResolver` /
ContextVar 绑定 / 项目路径 / sandbox），且 skill 摘要提示 agent "read the
SKILL.md file using the read_file tool" —— 目前工具侧还没有 read_file/file 权限
这类能力与门禁；同时 `ToolContext`（`workspace=""`、`config=None`）需要拿到
真实值，才能消费 step25 的 config（包括 skills 目录位置与 disabled_skills）。