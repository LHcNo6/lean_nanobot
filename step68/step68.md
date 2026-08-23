# step68：FindFilesTool + GrepTool 搜索工具

## 1. 问题背景

step65-67 实现了文件写入、编辑和目录列表。但 agent 仍缺少两个关键的搜索能力：
1. **文件查找**：按名称/glob/类型在项目中定位文件；
2. **内容搜索**：在文件中搜索正则/文本模式。

没有这两个工具，agent 只能用 `list_dir` 递归浏览 + `read_file` 逐个查看，
效率极低且消耗大量 token。nanobot 的 `search.py` 提供这两个工具，共享
`_SearchTool` 基类。

## 2. 原理分析

### 2.1 为什么用 `os.walk` 而不是 `Path.rglob`？

`os.walk` 允许在遍历过程中原地修改 `dirnames`（`dirnames[:] = [...]`），从而
高效地跳过噪声目录——不会进入 `.git` 等目录递归。`Path.rglob` 会先遍历所有
目录再过滤，效率较低，且无法避免进入大目录。

### 2.2 为什么需要二进制文件检测？

GrepTool 搜索文件内容时，二进制文件（如图片、编译产物）解码会失败或产生
无意义结果。`_is_binary` 通过检测 null 字节和非文本控制字符比例来判断，
跳过二进制文件避免错误和噪声。

### 2.3 为什么 glob 匹配分两种情况？

- 模式含 `/` 或以 `**` 开头 → 匹配完整相对路径（如 `tests/**/*.py`）；
- 否则 → 仅匹配文件名（如 `*.py`）。

这与用户直觉一致：`*.py` 是按文件名过滤，`src/**/*.py` 是按路径过滤。

### 2.4 为什么 `_SearchTool` 继承 `_FsTool`？

搜索工具需要：
- workspace 边界守卫（`_resolve`）；
- 文件状态追踪（`_file_states`，虽然搜索工具不写入，但基类要求）；
- 配置读取（`enabled`）。

继承 `_FsTool` 可以复用这些基础设施，避免重复实现。

### 2.5 为什么 GrepTool 限制文件大小 2MB？

大文件（如日志、数据导出）搜索会消耗大量内存和时间。2MB 是一个合理的阈值，
覆盖绝大多数源代码文件，同时避免异常大文件拖慢搜索。

## 3. 实现方案

### 3.1 辅助函数（`tools/search.py`）

- `_is_binary(raw)`：二进制检测；
- `_match_glob(rel_path, name, pattern)`：glob 匹配；
- `_matches_type(name, file_type)`：文件类型简写匹配；
- `_matches_query(display_path, query)`：路径片段搜索。

### 3.2 `_SearchTool` 基类

- `_IGNORE_DIRS`：复用 `ListDirTool._IGNORE_DIRS`；
- `_display_path(target, root)`：workspace-relative 路径显示；
- `_iter_files(root)`：`os.walk` + 噪声目录过滤。

### 3.3 `FindFilesTool`

参数：`path`、`query`、`glob`、`type`、`head_limit`
- 递归遍历，按 query/glob/type 过滤；
- 按路径排序；
- head_limit 截断。

### 3.4 `GrepTool`

参数：`pattern`、`path`、`glob`、`type`、`case_insensitive`、`fixed_strings`、
`output_mode`、`head_limit`
- 编译正则（支持 fixed_strings 和 case_insensitive）；
- 遍历文件，跳过二进制和 >2MB 文件；
- 两种输出模式：content（匹配行+行号）、files_with_matches（仅文件路径）；
- head_limit 截断。

### 3.5 `_FsTool` 扩展

新增 `_display_workspace()` 方法，返回当前 workspace Path，供搜索工具
计算相对路径。

## 4. 核心类/函数说明

### `_SearchTool`

搜索工具共享基类，继承 `_FsTool`。提供文件遍历和路径显示能力。

关键方法：
- `_iter_files(root)`：高效递归遍历（os.walk + 原地过滤噪声目录）；
- `_display_path(target, root)`：优先相对于 workspace 根，否则相对于搜索根。

### `FindFilesTool`

文件查找工具。支持路径片段搜索（query，空白分隔词全部匹配）、glob 模式过滤、
文件类型简写过滤。返回 workspace-relative 路径列表。

### `GrepTool`

内容搜索工具。支持正则和纯文本模式，不区分大小写选项，两种输出模式。
自动跳过二进制文件和 >2MB 大文件。

输出格式：
- content：`"{path}:{line_no}| {content}"`；
- files_with_matches：每行一个文件路径。

## 5. 文件修改清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `tools/filesystem.py` | 修改 | `_FsTool` + `_display_workspace()` 方法 |
| `tools/search.py` | 新建 | 辅助函数 + `_SearchTool` + `FindFilesTool` + `GrepTool` |
| `tests/test_search.py` | 新建 | 30 个单元测试 |
| `proposal.md` | 新建 | 需求定义 |
| `design.md` | 新建 | 架构设计 |
| `api-spec.md` | 新建 | 接口契约 |

## 6. 测试结果

- `tests/test_search.py`：30 passed（辅助函数/FindFiles/Grep/发现）
- `tests/test_filesystem.py`：19 passed（无回归）
- `tests/test_edit_file.py`：26 passed（无回归）
- `tests/test_list_dir.py`：15 passed（无回归）
- `tests/test_file_state.py`：21 passed（无回归）
- `tests/test_workspace_tool.py`：18 passed（无回归）
- **合计：129 passed**

## 7. 第一阶段总结（step65-68）

### 完成的工具

| Step | 工具 | 功能 |
|------|------|------|
| step65 | `write_file` | 创建/覆盖文件 |
| step66 | `edit_file` | 精确字符串替换 |
| step67 | `list_dir` | 目录列表（支持递归） |
| step68 | `find_files` | 按名称/glob/类型查找文件 |
| step68 | `grep` | 正则/纯文本内容搜索 |

### 基础设施

- `_FsTool` 基类：路径解析、文件状态追踪、workspace 边界守卫
- `_SearchTool` 基类：文件遍历、路径显示
- `FileStateStore`：按会话隔离的文件状态追踪
- `ToolContext.file_state_store`：工具上下文扩展

### 暴露的技术债

1. **ReadFileTool 仍在独立文件**：`tools/read_file.py` 与 `tools/filesystem.py`
   并存，路径解析逻辑有重复。后续应迁移到 filesystem.py 并升级到带行号分页版本。

2. **EditFileTool 缺少高级特性**：line_hint、引号风格保留、缩进保留、
   最佳匹配诊断、删除行尾随换行清理等。

3. **搜索工具缺少高级特性**：FindFilesTool 缺少 include_dirs/sort=modified/
   offset 分页；GrepTool 缺少 count 输出模式/context 上下文。

4. **ToolContext 仍缺少多个 nanobot 字段**：cron_service、exec_session_manager、
   provider_snapshot_loader、image_generation_provider_configs 等。

### 下一步方向

第二阶段（step69+）：执行与网络工具
- step69：`ExecTool` 基础版（shell 命令执行）
- step70：`ExecTool` 增强版（超时、环境变量、权限）
- step71：`WebFetchTool`（网页抓取）
- step72：`WebSearchTool`（网页搜索）
- step73：`ExecSession`（交互式执行会话）
