# Step 107 Design: build_dream_tools

## 实现思路

### 1. build_dream_tools 方法

```python
def build_dream_tools(self) -> list[dict]:
    """构建 Dream 运行使用的受限工具定义列表。"""
    workspace = self.workspace
    skills_dir = workspace / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    editable_files = [self.memory_file, self.soul_file, self.user_file]

    return [
        # read_file: 可读取整个工作区
        _build_read_file_tool(workspace=workspace),
        # write_file: 限 skills 目录
        _build_write_file_tool(workspace=workspace, allowed_dir=skills_dir),
        # edit_file: 限 skills 目录 + 记忆文件
        _build_edit_file_tool(
            workspace=workspace,
            allowed_dir=skills_dir,
            extra_write_allowed_files=editable_files,
        ),
    ]
```

### 2. 工具权限设计

| 工具 | 可读范围 | 可写范围 |
|------|---------|---------|
| read_file | 整个 workspace | 只读 |
| write_file | workspace | skills/ 目录 |
| edit_file | workspace | skills/ 目录 + SOUL.md + USER.md + memory/MEMORY.md |

记忆文件（SOUL.md / USER.md / MEMORY.md）通过 `extra_write_allowed_files` 白名单单独授权，不开放整个 workspace 写权限。

### 3. 工具定义格式

返回 OpenAI function calling 格式的工具定义列表，每个工具包含：
- `type: "function"`
- `function.name`：工具名
- `function.description`：工具描述
- `function.parameters`：JSON Schema 参数定义

### 4. 设计取舍

- **返回工具定义而非 ToolRegistry**：参考实现返回 ToolRegistry，但当前 step 的工具系统以 dict 定义为主，返回 list[dict] 更简洁，后续可扩展为 ToolRegistry。
- **不包含 ApplyPatch**：当前工具集没有 patch 工具，edit_file 已能满足记忆文件编辑需求。

## 文件修改清单

| 文件 | 操作 |
|------|------|
| `memory.py` | 修改：+build_dream_tools 方法 |
| `tests/test_build_dream_tools.py` | 新建（6 测试） |
| `proposal.md` / `design.md` / `api-spec.md` | 新建 |
