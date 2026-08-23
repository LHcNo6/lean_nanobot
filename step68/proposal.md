# Step 68 Proposal: FindFilesTool + GrepTool

## 1. 问题背景

step65-67 实现了文件写入、编辑和目录列表。但 agent 仍缺少两个关键的搜索能力：
1. **文件查找**：按名称/glob/类型在项目中定位文件（替代 shell `find`）；
2. **内容搜索**：在文件中搜索正则/文本模式（替代 shell `grep`）。

没有这两个工具，agent 只能用 `list_dir` 递归浏览 + `read_file` 逐个查看，
效率极低且消耗大量 token。

nanobot 的 `search.py` 提供 `FindFilesTool` 和 `GrepTool`，共享 `_SearchTool` 基类。

## 2. 目标

新建 `tools/search.py`，实现两个搜索工具：

### FindFilesTool（简化版）
- 参数：`path`、`query`（路径片段）、`glob`、`type`（文件类型简写）
- 递归遍历，自动过滤噪声目录
- 返回 workspace-relative 路径列表
- 支持 `head_limit` 截断

### GrepTool（简化版）
- 参数：`pattern`、`path`、`glob`、`type`、`case_insensitive`、`fixed_strings`、`output_mode`
- 支持两种输出模式：`content`（匹配行+行号）、`files_with_matches`（仅文件路径）
- 自动跳过二进制文件和 >2MB 的大文件
- 支持 `head_limit` 截断

### _SearchTool 基类
- 继承 `_FsTool`
- 共享 `_iter_files(root)`：`os.walk` + 噪声目录过滤
- 共享 `_display_path(target, root)`：workspace-relative 路径输出

## 3. 非目标（明确不做）

- **不实现** `include_dirs`（FindFilesTool）—— 只返回文件
- **不实现** `sort=modified`（FindFilesTool）—— 只按路径排序
- **不实现** `offset` 分页 —— 只支持 head_limit 截断
- **不实现** `count` 输出模式（GrepTool）—— 只支持 content 和 files_with_matches
- **不实现** `context_before`/`context_after`（GrepTool）—— 只返回匹配行
- **不实现** `_TYPE_GLOB_MAP` 完整类型映射 —— 只支持基础类型（py/js/ts/md/json/yaml等）
- **不实现** `max_matches`/`max_results`  legacy 别名 —— 只用 head_limit

## 4. 方案选择

### 方案 A：两个工具各自实现遍历逻辑
- 优点：无基类依赖
- 缺点：`os.walk` + 噪声过滤逻辑重复

### 方案 B：_SearchTool 基类 + 两个工具（选定）
- 优点：共享遍历逻辑，与 nanobot 架构一致
- 缺点：多一个基类

**选择方案 B**。两个工具共享文件遍历逻辑，基类是必要的基础设施。

## 5. 关键设计决策

### 5.1 文件遍历：`os.walk` 而非 `Path.rglob`
`os.walk` 允许在遍历过程中修改 `dirnames`（`dirnames[:] = [...]`），从而高效地
跳过噪声目录（不会进入 `.git` 等目录递归）。`Path.rglob` 会先遍历所有目录再过滤，
效率较低。

### 5.2 二进制文件检测：`_is_binary`
```python
def _is_binary(raw: bytes) -> bool:
    if b"\x00" in raw:
        return True
    sample = raw[:4096]
    non_text = sum(byte < 9 or 13 < byte < 32 for byte in sample)
    return (non_text / len(sample)) > 0.2
```
检测 null 字节和非文本控制字符比例，与 nanobot 一致。

### 5.3 glob 匹配：`_match_glob`
- 模式含 `/` 或以 `**` 开头：用 `PurePosixPath(rel_path).match(pattern)` 匹配完整路径；
- 否则：用 `fnmatch.fnmatch(name, pattern)` 匹配文件名。

### 5.4 类型简写：`_matches_type`
简化版类型映射：
```python
_TYPE_GLOB_MAP = {
    "py": ("*.py", "*.pyi"),
    "js": ("*.js", "*.jsx", "*.mjs", "*.cjs"),
    "ts": ("*.ts", "*.tsx", "*.mts", "*.cts"),
    "md": ("*.md", "*.mdx"),
    "json": ("*.json",),
    "yaml": ("*.yaml", "*.yml"),
    "toml": ("*.toml",),
    "html": ("*.html", "*.htm"),
    "css": ("*.css",),
    "sh": ("*.sh", "*.bash"),
}
```
未在映射中的类型自动用 `*.{type}` 匹配。

## 6. 验收标准

1. `FindFilesTool` 和 `GrepTool` 可被 `ToolLoader` 自动发现
2. FindFilesTool 按 query/glob/type 过滤文件
3. GrepTool content 模式返回匹配行（含行号）
4. GrepTool files_with_matches 模式返回匹配文件路径
5. 噪声目录被过滤
6. 二进制文件被跳过
7. 超过 head_limit 时截断提示
8. 所有现有测试通过，新增单元测试
