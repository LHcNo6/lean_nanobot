# Step 107 Proposal: build_dream_tools

## 1. 问题背景

Dream 运行时使用默认的完整工具集（包含 shell exec、网络请求等危险工具），存在安全风险。nanobot 的 Dream 运行使用受限工具集，只允许读取工作区文件和编辑记忆文件（SOUL.md / USER.md / memory/MEMORY.md）及 skills 目录。

## 2. 目标

新增 `build_dream_tools()` 方法，返回 Dream 运行专用的受限工具定义列表（OpenAI 格式），包含：
- `read_file`：读取工作区文件
- `write_file`：写入文件（限 skills 目录 + 记忆文件）
- `edit_file`：替换文件内容（限 skills 目录 + 记忆文件）

不包含 shell/exec/网络等危险工具。

## 3. 非目标

- 不实现实际的工具执行逻辑（复用现有 tools 模块）
- 不修改 Dream 运行流程（main.run_dream 后续集成）
- 不实现 ApplyPatchTool（当前工具集无此工具）

## 4. 验收标准

1. `build_dream_tools()` 返回非空列表
2. 列表包含 read_file 工具定义
3. 列表包含 write_file 工具定义
4. 列表包含 edit_file 工具定义
5. 不包含 shell / exec / 网络相关工具
6. 每个工具定义有 name / description / parameters 字段
7. 工具的 allowed_dir / extra_write_allowed_files 限制正确
8. 单元测试通过
