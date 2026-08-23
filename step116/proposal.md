# Step116 需求定义：子代理 system prompt 模板化（workspace + skills_summary）

## 1. 问题背景

step115 已让 `run_cli_app` 在子代理中可用。但子代理的 system prompt 仍是 step115 里硬编码的
3 行文本（`subagent.py:135` 的 `_SUBAGENT_SYSTEM_PROMPT`），**没有任何 workspace 与 skills 信息**——
子代理不知道自己处于哪个工作区、有哪些技能可用。主代理（`context.py:217-239`）早已注入
`# Workspace` / `# Skills` / `# Active Skills` 段，子代理却缺失，行为不对等。

## 2. 本 step 要解决什么

把硬编码的子代理 prompt 模板化，渲染出「workspace 路径」+「可用技能摘要（skills_summary）」，
对齐 nanobot `subagent_system.md` 的角色设定，使子代理能感知工作区与技能。

## 3. 为什么这样做（方案取舍）

- 方案 A「在 `_run_subagent` 里用 f-string 直接拼 workspace + skills」：可行但把渲染逻辑混在
  运行体内，难以单测、难以扩展。**否决**。
- 方案 B（选定）「模块内模板常量 + 独立渲染函数 + `SubagentManager._build_subagent_system_prompt`
  方法」：渲染逻辑与运行体解耦，便于单测；措辞对齐主代理 `# Skills` 段；技能由既有的
  `SkillsLoader.build_skills_summary()` 提供，零重写。

## 4. 目标与实现边界（最小增量）

- 目标：子代理 prompt 含 workspace 与可用技能摘要；无技能时省略该段。
- 边界（**不做**）：
  - 不引入外部模板文件（选「模块内模板常量」以降低依赖，见 step116 技术决策）；
  - 不注入 `# Active Skills` 全量内容（子代理仅给渐进加载摘要，与主代理一致的最小形态）；
  - 不改主代理 prompt、不改 `SkillsLoader` 语义。

## 5. 验收标准

1. `subagent.py` 新增 `_render_subagent_system_prompt(workspace, skills_summary)` 与
   `SubagentManager._build_subagent_system_prompt(workspace)`。
2. `SubagentManager.__init__` 构造 `SkillsLoader(workspace, disabled_skills=...)` 并从原始
   `config` 提取 `disabled_skills`。
3. `_run_subagent` 用渲染后的 prompt 替换原硬编码 `_SUBAGENT_SYSTEM_PROMPT`。
4. 测试：渲染函数（含/不含 skills 段）；`SubagentManager` 在有/无 skills 目录的 workspace 下、
   以及 `disabled_skills` 生效时输出正确的 prompt。
5. 全量测试失败数与 step115 基线（25）持平，无新增回归。
