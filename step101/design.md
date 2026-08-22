# Step 101 Design: workspace_prompts 模块 + MemoryStore dream 模板方法

## 实现思路

### 1. utils/workspace_prompts.py 模块

提供 4 个函数 + 1 个常量：

- `WORKSPACE_PROMPT_MAX_CHARS = 16000`：workspace prompt 最大字符数
- `workspace_prompt_file(workspace, name) -> Path`：返回 `workspace / ".prompts" / f"{name}.md"`
- `has_workspace_prompt_override(path) -> bool`：文件存在且 `stat().st_size > 0`
- `load_workspace_prompt_override(path) -> tuple[str | None, int]`：读取文件，超限用 `truncate_text` 截断，返回 `(text, original_chars)`；文件不存在/空返回 `(None, 0)`
- `initialize_workspace_prompt(workspace, name, default_content)`：文件不存在时写入默认内容

### 2. MemoryStore 新增方法

- `dream_prompt_file`（property）：返回 `workspace_prompt_file(self.workspace, "dream")`
- `has_dream_prompt_override() -> bool`：委托 `has_workspace_prompt_override`
- `default_dream_prompt() -> str`（静态方法）：返回硬编码的默认 Dream 系统提示
- `_dream_template() -> str`：优先加载 workspace 覆盖，超限记录 warning（限流），否则返回默认模板

### 3. 超限日志限流

新增 `_dream_prompt_oversize_logged: bool` 实例字段，首次超限时设为 True 并 `logger.warning`，后续跳过。

## 文件修改清单

| 文件 | 操作 |
|------|------|
| `utils/workspace_prompts.py` | 新建 |
| `memory.py` | 修改：+导入 +flag +4 个 dream 模板方法 |
| `tests/test_dream_template.py` | 新建（16 测试） |
| `proposal.md` / `design.md` / `api-spec.md` | 新建 |
