# Step 67 Proposal: ListDirTool

## 1. 问题背景

step65-66 实现了文件写入和编辑，但 agent 在操作文件前需要了解目录结构——
有哪些文件、哪些子目录。当前 learn_nano 没有目录列表工具，agent 只能通过
`read_file` 逐个尝试，效率极低。

nanobot 的 `ListDirTool` 提供目录列表功能，支持递归遍历、噪声目录自动过滤、
结果截断。这是 agent 探索项目结构的基础工具。

## 2. 目标

在 `tools/filesystem.py` 中新增 `ListDirTool`：

1. 核心参数：`path`、`recursive`、`max_entries`
2. 非递归模式：列出目录直接子项
3. 递归模式：遍历所有子目录和文件
4. 自动过滤噪声目录（`.git`、`node_modules`、`__pycache__` 等）
5. 结果超过 `max_entries` 时截断并提示
6. 目录/文件类型标识（不用 emoji，用 `/` 后缀区分目录）

## 3. 非目标（明确不做）

- **不实现** 文件大小/修改时间显示 —— 后续增强
- **不实现**  glob 模式过滤 —— 用 `find_files` 工具（step68）
- **不实现** 隐藏文件显示开关 —— 默认显示所有非噪声目录
- **不使用** emoji 图标 —— 保持与 learn_nano 其他工具一致的纯文本输出

## 4. 方案选择

### 方案 A：`os.listdir()` + 手动递归
- 优点：无依赖
- 缺点：递归需手写，排序需手动处理

### 方案 B：`Path.iterdir()` + `Path.rglob()`（选定）
- 优点：代码简洁，`rglob` 内置递归，`sorted()` 排序
- 缺点：无

**选择方案 B**，与 nanobot 实现一致。

## 5. 关键设计决策

### 5.1 路径解析：用 `_resolve_read` 而非 `_resolve_write`
列目录是只读操作，可以享受读豁免目录（如内置技能目录）。复用 `_FsTool._resolve`
（即 `_resolve_read`）。

### 5.2 噪声目录过滤
```python
_IGNORE_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", ".tox", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".coverage", "htmlcov",
}
```
- 非递归：检查 `item.name in _IGNORE_DIRS`
- 递归：检查 `any(p in _IGNORE_DIRS for p in item.parts)`（路径中任何一段是噪声目录则跳过）

### 5.3 输出格式
- 非递归：`{name}/` 表示目录，`{name}` 表示文件
- 递归：`{relative_path}/` 表示目录，`{relative_path}` 表示文件
- 截断：末尾追加 `(truncated, showing first {cap} of {total} entries)`

### 5.4 空目录处理
目录为空时返回 `"Directory {path} is empty"`。

## 6. 验收标准

1. `ListDirTool` 可被 `ToolLoader` 自动发现并注册
2. 非递归模式列出直接子项，目录带 `/` 后缀
3. 递归模式遍历所有子项
4. 噪声目录被过滤
5. 超过 `max_entries` 时截断并提示
6. 空目录返回空目录消息
7. 路径不存在/不是目录返回错误
8. `read_only=True`
9. 所有现有测试通过，新增 `ListDirTool` 单元测试
