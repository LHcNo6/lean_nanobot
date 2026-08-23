# Step116 架构设计：子代理 system prompt 模板化（workspace + skills_summary）

## 1. 总体思路

复用 step115 已就绪的 `SkillsLoader.build_skills_summary()`（返回 markdown 技能列表或空串），
把子代理的硬编码 prompt 改为「base 文本 + `# Workspace` 段 + 可选的 `# Skills` 段」渲染结果，
与主代理 `ContextBuilder.build_system_prompt`（`context.py:217-239`）的 `# Skills` 措辞保持一致。
渲染逻辑抽到独立函数/方法，便于单测。

## 2. 模板（模块内常量，subagent.py）

保留原 base 文本为模块级常量，避免破坏既有语义：

```python
_SUBAGENT_SYSTEM_PROMPT = """You are a subagent spawned by the main agent.
Stay focused on the assigned task. Use tools to complete it.
Your final response will be reported back to the main agent."""
```

渲染函数：

```python
def _render_subagent_system_prompt(workspace: str, skills_summary: str) -> str:
    parts = [
        _SUBAGENT_SYSTEM_PROMPT,
        f"# Workspace\n\nYou are operating within the workspace: {workspace}",
    ]
    if skills_summary:
        parts.append(
            "# Skills\n\n"
            "The following skills extend your capabilities. To use a skill, "
            "read its SKILL.md file using the read_file tool. "
            "Unavailable skills need dependencies installed first.\n\n"
            + skills_summary
        )
    return "\n\n---\n\n".join(parts)
```

- `workspace` 取运行期生效的项目根（`ws_scope.project_path` 优先，回落 `self._workspace`）。
- `skills_summary` 为空（无技能/全部禁用）时省略 `# Skills` 段，避免无意义噪声。

## 3. 子代理技能加载器（SubagentManager.__init__）

- 从原始 `config` 实参安全提取 `disabled_skills`（duck-typed `getattr` 链，缺省 `set()`）：
  ```python
  self._disabled_skills = _extract_disabled_skills(config)
  ```
  注意：`_flatten_tools_config` 只暴露 `web/exec/tools`，**不携带 `agents` 段**，
  因此必须在 `__init__` 里直接从原始 `config` 提取。
- 构造加载器（叶子模块，无循环依赖）：
  ```python
  from step116.skills.loader import SkillsLoader
  self._skills_loader = SkillsLoader(
      workspace=self._workspace, disabled_skills=self._disabled_skills)
  ```
- 新增方法：
  ```python
  def _build_subagent_system_prompt(self, workspace: str) -> str:
      summary = self._skills_loader.build_skills_summary()
      return _render_subagent_system_prompt(workspace, summary)
  ```

## 4. 接入 _run_subagent

原 `subagent.py:303-306`：

```python
messages = [
    {"role": "system", "content": _SUBAGENT_SYSTEM_PROMPT},
    {"role": "user", "content": task},
]
```

改为：

```python
ws_path = str(ws_scope.project_path) if ws_scope else str(self._workspace)
system_prompt = self._build_subagent_system_prompt(ws_path)
messages = [
    {"role": "system", "content": system_prompt},
    {"role": "user", "content": task},
]
```

其中 `ws_scope = origin.get("workspace_scope")`（已在 `_run_subagent` 内计算，line 318）。

## 5. 数据流

```
config.agents.defaults.disabled_skills
   └─ _extract_disabled_skills(config) ─► SubagentManager._disabled_skills
        └─ SkillsLoader(workspace, disabled_skills) ─► _skills_loader
             └─ _build_subagent_system_prompt(ws_path)
                  ├─ _SUBAGENT_SYSTEM_PROMPT (base)
                  ├─ "# Workspace: <ws_path>"
                  └─ "# Skills: <build_skills_summary()>"  (非空时)
```

## 6. 利弊与风险

- 利：子代理 prompt 角色设定与主代理对齐；可感知工作区与技能；渲染逻辑可单测。
- 风险/注意：
  - `disabled_skills` 提取依赖 `config` 实参在 `__init__` 仍可见（不变），若将来重构移除法，
    需另行传递。
  - 仅注入渐进加载摘要（不含 `# Active Skills` 全量内容），与子代理「最小能力暴露」一致；
    若未来需 always 技能全量注入，可扩展 `_build_subagent_system_prompt` 调
    `load_skills_for_context(get_always_skills())`。

## 7. 不在本 step 范围

- step117：子代理运行时限制（llm_timeout）同步；
- step118：microcompaction 工具集对齐；
- step119：self/my 工具子代理状态可观测。
