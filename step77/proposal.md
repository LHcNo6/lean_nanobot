# Step 77 Proposal: GlobTool

## 1. 问题背景

FindFilesTool 支持按名称/类型/扩展名搜索，但不支持灵活的 glob 模式匹配（如 `**/*.py`、`src/**/test_*.py`）。
nanobot 有独立的 GlobTool 提供标准 glob 模式匹配能力。

## 2. 目标

新建 `tools/glob_tool.py`，实现 GlobTool：
1. 支持标准 glob 模式（`*`, `?`, `**`, `[seq]`）
2. 递归匹配（`**`）
3. 结果按相对路径输出（as_posix）
4. 限制最大结果数（默认 200）
5. 继承 _FsTool，共享路径解析和边界检查

## 3. 非目标

- 不实现正则匹配
- 不实现内容搜索（已有 GrepTool）

## 4. 验收标准

1. GlobTool 可被 ToolLoader 发现
2. `*.py` 匹配当前目录 .py 文件
3. `**/*.py` 递归匹配
4. 结果数不超过 max_results
5. 单元测试通过
