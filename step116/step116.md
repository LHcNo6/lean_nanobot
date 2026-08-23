# Step116：子代理 system prompt 模板化（workspace + skills_summary）

## 1. 问题背景

step115 已让子代理能跑 `run_cli_app` 等 13 个工具，但子代理的 system prompt 仍是 step115 里硬编码的
3 行文本（`subagent.py` 的 `_SUBAGENT_SYSTEM_PROMPT`），**没有任何 workspace 与 skills 信息**。主代理
（`context.py` 的 `ContextBuilder.build_system_prompt`）早已注入 `# Workspace` / `# Skills` 段，子代理缺失，
行为不对等。

## 2. 这一 step 解决了什么 / 为什么这样做

把硬编码 prompt 改为**模板化渲染**：base 角色设定 + 工作区根路径 + 可用技能摘要。技能直接复用既有的
`SkillsLoader.build_skills_summary()`（返回 markdown 列表或空串），零重写。

方案取舍：
- 否决「在 `_run_subagent` 里用 f-string 直接拼」——渲染逻辑混在运行体内，难单测、难扩展。
- 选定「**模块内模板常量** + 独立渲染函数 + `SubagentManager._build_subagent_system_prompt`
  方法」（用户确认用模块内常量，不引入外部模板文件，保持最小增量、少依赖）。
- 仅注入渐进加载摘要（`# Skills` 段），不注入 `# Active Skills` 全量内容——与子代理「最小能力暴露」一致。

## 3. 原理思路与具体实现

### 3.1 模板（subagent.py 模块级）
保留原 base 常量，新增渲染函数：
```python
def _render_subagent_system_prompt(workspace: str, skills_summary: str) -> str:
    parts = [
        _SUBAGENT_SYSTEM_PROMPT,
        f"# Workspace\n\nYou are operating within the workspace: {workspace}",
    ]
    if skills_summary and skills_summary.strip():
        parts.append(
            "# Skills\n\n"
            "The following skills extend your capabilities. To use a skill, "
            "read its SKILL.md file using the read_file tool. "
            "Unavailable skills need dependencies installed first.\n\n"
            + skills_summary
        )
    return "\n\n---\n\n".join(parts)
```
`skills_summary` 为空（无技能 / 全部禁用）时省略 `# Skills` 段，避免噪声。

### 3.2 子代理技能加载器（SubagentManager.__init__）
- 从原始 `config` 实参安全提取 `disabled_skills`（duck-typed `getattr` 链，缺省 `set()`）：
  ```python
  self._disabled_skills = _extract_disabled_skills(config)
  ```
  > 注意：`_flatten_tools_config` 只暴露 `web/exec/tools`，**不携带 `agents` 段**，
  > 因此必须在 `__init__` 里直接从原始 `config` 提取。
- 构造加载器（叶子模块，无循环依赖）：
  ```python
  from step116.skills.loader import SkillsLoader
  self._skills_loader = SkillsLoader(workspace=self._workspace, disabled_skills=self._disabled_skills)
  ```

### 3.3 接入 _run_subagent
```python
ws_scope = origin.get("workspace_scope")
ws_path = str(ws_scope.project_path) if ws_scope else str(self._workspace)
system_prompt = self._build_subagent_system_prompt(ws_path)
messages = [
    {"role": "system", "content": system_prompt},
    {"role": "user", "content": task},
]
```
`# Skills` 摘要由运行期 `self._skills_loader.build_skills_summary()` 生成（含内置技能，如 memory/weather）。

## 4. 核心函数 / 类功能说明

| 元素 | 职责 |
| --- | --- |
| `_render_subagent_system_prompt(workspace, skills_summary)` | 模块级纯函数：拼接 base + Workspace + 可选 Skills |
| `_extract_disabled_skills(config)` | 从原始 config 安全提取禁用技能名集合（多形态回退） |
| `SubagentManager._build_subagent_system_prompt(workspace)` | 取技能摘要并渲染，供 `_run_subagent` 注入 |
| `SubagentManager.__init__` 新增 `self._skills_loader` | 持有子代理专属 `SkillsLoader` |

## 5. 暴露了什么问题 / 下一 step

- 暴露：子代理现在会看到**内置技能**（如 memory/weather）——这是期望的对齐行为，但意味着
  `# Skills` 段几乎总会渲染（除非禁用全部）。`skills_summary` 为空的分支在正常环境是死分支，仅作为防御。
- 暴露：仅注入渐进摘要，未注入 `always` 技能的**全量内容**。若某些 always 技能（如 memory）需要预先
  注入全文，未来可在 `_build_subagent_system_prompt` 调 `load_skills_for_context(get_always_skills())`。
- 下一 step（step117）：子代理运行时限制（如 `llm_timeout`）与主代理对齐。

## 6. 验证

- 新增 `tests/test_subagent_prompt.py`：8 个用例全绿。
  - 渲染函数：含 workspace；含/不含 `# Skills` 段；
  - 管理器：含技能目录的工作区输出含 workspace + `# Skills` + 技能名；内置技能始终注入；
  - `disabled_skills` 生效排除指定技能、保留内置；
  - `_extract_disabled_skills` 多形态（完整 Config / None / 扁平 view）回退。
- 全量 `step116/tests`：**25 failed / 1155 passed**（与 step115 基线 25 持平，新增 8 通过，无新增回归）。
  失败用例为 Windows 既有问题，与子代理 prompt 无关。
