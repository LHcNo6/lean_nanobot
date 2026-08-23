# Step116 接口契约（api-spec）

本文件定义 step116「子代理 system prompt 模板化」的对外契约，供实现与测试对齐。

## D1：渲染函数（subagent.py，模块级）

```python
def _render_subagent_system_prompt(workspace: str, skills_summary: str) -> str: ...
```

契约：
- 输出必含：base 文本（`_SUBAGENT_SYSTEM_PROMPT`）+ `# Workspace` 段（含 `workspace`）。
- `skills_summary` 非空（去空白后非空串）→ 追加 `# Skills` 段，措辞与主代理 `context.py:217-239` 一致；
- `skills_summary` 为空 → 省略 `# Skills` 段。
- 段间以 `"\n\n---\n\n"` 连接（与主代理 `build_system_prompt` 同款分隔）。

## D2：子代理管理器方法

```python
class SubagentManager:
    def _build_subagent_system_prompt(self, workspace: str) -> str: ...
```

契约：
- 内部调用 `self._skills_loader.build_skills_summary()` 取得摘要，再交给 D1 渲染；
- 返回的 prompt 即注入 `messages[0]["role"=="system"]` 的内容。

## D3：构造期依赖（SubagentManager.__init__）

- 从原始 `config` 实参提取 `disabled_skills`：
  `config.agents.defaults.disabled_skills`（duck-typed 安全链，缺省 `set()`）。
- 构造 `self._skills_loader = SkillsLoader(workspace=self._workspace, disabled_skills=self._disabled_skills)`。

## D4：_run_subagent 行为

- `ws_path = str(ws_scope.project_path) if ws_scope else str(self._workspace)`；
- `messages[0]["content"] = self._build_subagent_system_prompt(ws_path)`（替换原硬编码 `_SUBAGENT_SYSTEM_PROMPT`）。

## D5：测试映射

| 契约 | 测试 |
| --- | --- |
| D1 | `_render_subagent_system_prompt`：含 workspace；含 skills 段（非空）/ 省略（空） |
| D2+D3 | `SubagentManager` 在含 `skills/<demo>/SKILL.md` 的临时 workspace 下，`_build_subagent_system_prompt` 输出含 workspace 路径与 `# Skills`+`demo` |
| D3 | `disabled_skills=["demo"]` 时该技能被排除出摘要 |
| D4 | `_run_subagent` 注入渲染后的 system prompt（经 fake_runner 捕获 spec 验证 messages[0] 含 workspace/skills） |

> 全部测试使用 mock / 构造数据，禁止真实网络与 API 调用。
