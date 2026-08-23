# step67：ListDirTool 目录列表

## 1. 问题背景

step65-66 实现了文件写入和编辑，但 agent 在操作文件前需要了解目录结构——
有哪些文件、哪些子目录。当前 learn_nano 没有目录列表工具，agent 只能通过
`read_file` 逐个尝试，效率极低，且无法发现未知文件。

nanobot 的 `ListDirTool` 提供目录列表功能，支持递归遍历、噪声目录自动过滤、
结果截断。这是 agent 探索项目结构的基础工具，也是"先了解再操作"工作流的前提。

## 2. 原理分析

### 2.1 为什么用 `Path.rglob()` 而不是 `os.walk()`？

`Path.rglob("*")` 是 `Path.glob("**/*")` 的简写，返回所有子项的 Path 对象，
代码简洁且支持 `sorted()` 排序。`os.walk()` 需要手动处理目录/文件分类，代码更冗长。

### 2.2 为什么需要噪声目录过滤？

项目中常见的 `.git`、`node_modules`、`__pycache__` 等目录包含大量自动生成的
文件，对 agent 没有价值且会导致输出爆炸。自动过滤这些目录可以：
- 减少 token 消耗；
- 避免 agent 误入生成代码目录；
- 加快目录遍历速度。

### 2.3 为什么递归模式的过滤逻辑不同？

- 非递归：只检查 `item.name in _IGNORE_DIRS`（直接子项的名称）；
- 递归：检查 `any(p in _IGNORE_DIRS for p in item.parts)`（路径中任何一段是
  噪声目录则跳过，避免列出 `.git/objects/pack/` 等深层子项）。

### 2.4 为什么用 `as_posix()` 输出路径？

Windows 下 `Path` 的字符串表示用反斜杠 `\`，而 Linux/macOS 用正斜杠 `/`。
使用 `as_posix()` 保证输出在所有平台上一致，agent 看到的路径格式统一，
复制到 `read_file`/`edit_file` 等工具时也不会因分隔符差异出问题。

### 2.5 为什么目录项带 `/` 后缀？

纯文本输出中，目录和文件难以区分。带 `/` 后缀是 Unix `ls -p` 的传统做法，
简洁且无歧义。不使用 emoji 是因为 learn_nano 其他工具均为纯文本输出，
保持一致性。

## 3. 实现方案

### 3.1 _FsTool 基类扩展

新增 `_resolve_read` 和 `_resolve` 方法：
- `_resolve_read(path)`：读路径解析（当前与 `_resolve_write` 相同，未来可扩展豁免目录）；
- `_resolve(path)`：默认路径解析（读语义），对齐 nanobot。

### 3.2 ListDirTool

```python
class ListDirTool(_FsTool):
    _DEFAULT_MAX = 200
    _IGNORE_DIRS = frozenset({".git", "node_modules", "__pycache__", ...})

    async def execute(self, path="", recursive=False, max_entries=None, **kwargs):
        dp = self._resolve(path)  # 读路径解析
        # 存在性检查
        # 遍历（iterdir / rglob），过滤噪声目录
        # 截断，组装结果
```

### 3.3 输出格式

- 非递归：`{name}/`（目录）或 `{name}`（文件），每行一个；
- 递归：`{relative_path}/`（目录）或 `{relative_path}`（文件），用 `as_posix()`；
- 截断：末尾追加 `(truncated, showing first {cap} of {total} entries)`；
- 空目录：`Directory {path} is empty`。

## 4. 核心类/函数说明

### `ListDirTool`

目录列表工具，继承 `_FsTool`。

关键特性：
- 支持非递归和递归两种模式；
- 自动过滤 13 种常见噪声目录；
- 可配置最大返回条目数（默认 200）；
- 只读操作（`read_only=True`）；
- 跨平台一致的正斜杠路径输出。

参数：
- `path`（必填）：要列出的目录路径；
- `recursive`（可选）：是否递归，默认 False；
- `max_entries`（可选）：最大返回条目数，默认 200。

### `_FsTool._resolve_read` / `_FsTool._resolve`

新增的读路径解析方法。当前与 `_resolve_write` 实现相同，但为未来扩展读豁免目录
（如内置技能目录）预留了接口。`ListDirTool` 使用 `_resolve`（读语义）。

## 5. 文件修改清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `tools/filesystem.py` | 修改 | `_FsTool` + `_resolve_read`/`_resolve`；新增 `ListDirTool` |
| `tests/test_list_dir.py` | 新建 | 15 个单元测试 |
| `proposal.md` | 新建 | 需求定义 |
| `design.md` | 新建 | 架构设计 |
| `api-spec.md` | 新建 | 接口契约 |

## 6. 测试结果

- `tests/test_list_dir.py`：15 passed（基础/递归/过滤/截断/错误/发现）
- `tests/test_filesystem.py`：19 passed（无回归）
- `tests/test_edit_file.py`：26 passed（无回归）
- `tests/test_file_state.py`：21 passed（无回归）
- `tests/test_workspace_tool.py`：18 passed（无回归）

## 7. 暴露问题与下一步

### 7.1 暴露的技术债

1. **缺少文件元信息**：nanobot 的 ListDirTool 输出带 emoji 图标，未来可增加
   文件大小、修改时间等元信息（当前仅名称）。

2. **缺少 glob 过滤**：无法按模式过滤（如 `*.py`），这个功能由 step68 的
   `FindFilesTool` 提供。

3. **_resolve_read 与 _resolve_write 实现相同**：当前 learn_nano 简化版没有
   读豁免目录（如内置技能目录）。未来 ReadFileTool 迁移到 filesystem.py 时，
   需要在 `_resolve_read` 中添加 `extra_allowed_roots`。

4. **ReadFileTool 仍在独立文件**：`tools/read_file.py` 与 `tools/filesystem.py`
   并存，路径解析逻辑有重复。

### 7.2 下一步（step68）

**FindFilesTool + GrepTool**：在 `tools/search.py` 中新增文件查找和内容搜索工具。
- `FindFilesTool`：按 glob/名称模式递归查找文件；
- `GrepTool`：正则表达式内容搜索。
两个工具共享 `_SearchTool` 基类，依赖 step67 的目录遍历经验和 step65 的
workspace 安全模型。
