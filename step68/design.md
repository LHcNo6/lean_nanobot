# Step 68 Design: FindFilesTool + GrepTool

## 1. 架构概览

```
tools/search.py（新建）
  ├── _is_binary()            函数：二进制文件检测
  ├── _match_glob()           函数：glob 模式匹配
  ├── _matches_type()         函数：文件类型简写匹配
  ├── _SearchTool(_FsTool)    基类：共享文件遍历 + 路径显示
  ├── FindFilesTool(_SearchTool)   文件查找工具
  └── GrepTool(_SearchTool)        内容搜索工具
```

## 2. 模块详细设计

### 2.1 辅助函数

#### `_is_binary(raw: bytes) -> bool`
检测文件内容是否为二进制：
- 含 null 字节 → 二进制；
- 前 4096 字节中非文本控制字符比例 > 20% → 二进制。

#### `_match_glob(rel_path: str, name: str, pattern: str) -> bool`
glob 模式匹配：
- 模式含 `/` 或以 `**` 开头 → `PurePosixPath(rel_path).match(pattern)`；
- 否则 → `fnmatch.fnmatch(name, pattern)`。

#### `_matches_type(name: str, file_type: str | None) -> bool`
文件类型简写匹配：
- `file_type` 为空 → True；
- 在 `_TYPE_GLOB_MAP` 中 → 用对应 glob 列表匹配；
- 不在 → 用 `*.{file_type}` 匹配。

### 2.2 `_SearchTool` 基类

```python
class _SearchTool(_FsTool):
    _IGNORE_DIRS = set(ListDirTool._IGNORE_DIRS)  # 复用噪声目录列表

    def _display_path(self, target: Path, root: Path) -> str:
        """返回 workspace-relative 路径（正斜杠）。"""
        workspace = self._display_workspace()
        if workspace:
            with suppress(ValueError):
                return target.relative_to(workspace).as_posix()
        return target.relative_to(root).as_posix()

    def _iter_files(self, root: Path) -> Iterable[Path]:
        """递归遍历文件，跳过噪声目录。"""
        if root.is_file():
            yield root
            return
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in self._IGNORE_DIRS)
            current = Path(dirpath)
            for filename in sorted(filenames):
                yield current / filename
```

注意：`_FsTool` 需要添加 `_display_workspace()` 方法（nanobot 有，learn_nano 当前没有）。
简化版直接返回 `self._workspace` 对应的 Path。

### 2.3 `FindFilesTool`

#### 参数
```python
@tool_parameters(tool_parameters_schema(
    path=StringSchema("Directory or file to search in (default '.')"),
    query=StringSchema("Case-insensitive path fragment search (whitespace-separated terms)"),
    glob=StringSchema("File filter, e.g. '*.py' or 'tests/**/test_*.py'"),
    type=StringSchema("File type shorthand, e.g. 'py', 'ts', 'md', 'json'"),
    head_limit=IntegerSchema("Maximum paths to return (default 200, 0 for all)", minimum=0),
    required=[],
))
```

#### execute 流程
1. `target = self._resolve(path or ".")`
2. 存在性检查
3. `root = target if target.is_dir() else target.parent`
4. 遍历 `_iter_files(target)`：
   - `rel_path = candidate.relative_to(root).as_posix()`
   - `display_path = self._display_path(candidate, root)`
   - glob 过滤 → `_match_glob`
   - type 过滤 → `_matches_type`
   - query 过滤 → 所有空白分隔词都在 display_path 中（不区分大小写）
5. 按路径排序
6. `head_limit` 截断（0 表示不限制）
7. 返回 `"\n".join(paths)`，截断时追加提示

### 2.4 `GrepTool`

#### 参数
```python
@tool_parameters(tool_parameters_schema(
    pattern=StringSchema("Regex or plain text pattern to search for"),
    path=StringSchema("File or directory to search in (default '.')"),
    glob=StringSchema("File filter, e.g. '*.py'"),
    type=StringSchema("File type shorthand, e.g. 'py', 'md'"),
    case_insensitive=BooleanSchema("Case-insensitive search (default false)"),
    fixed_strings=BooleanSchema("Treat pattern as plain text (default false)"),
    output_mode=StringSchema("'content' (matching lines) or 'files_with_matches' (default)",
                              enum=["content", "files_with_matches"]),
    head_limit=IntegerSchema("Maximum results (default 250, 0 for all)", minimum=0),
    required=["pattern"],
))
```

#### execute 流程
1. `target = self._resolve(path or ".")`
2. 存在性检查
3. 编译正则：`fixed_strings` 时 `re.escape(pattern)`，`case_insensitive` 时加 `re.IGNORECASE`
4. 遍历 `_iter_files(target)`：
   - glob/type 过滤
   - 读取 bytes，>2MB 跳过，二进制跳过
   - 解码 UTF-8
   - 逐行匹配正则
   - `files_with_matches` 模式：记录匹配文件路径
   - `content` 模式：记录 `"{path}:{line_no}| {line}"`
5. `head_limit` 截断
6. 返回结果

#### 输出格式
- content 模式：每行 `"{display_path}:{line_no}| {content}"`
- files_with_matches 模式：每行 `"{display_path}"`

## 3. _FsTool 扩展

需要在 `_FsTool` 中添加 `_display_workspace()` 方法：
```python
def _display_workspace(self) -> Path | None:
    """返回当前 workspace 的 Path（用于显示相对路径）。"""
    access = current_tool_workspace(self._workspace)
    return access.project_path
```

## 4. 错误处理

| 场景 | 返回消息 |
|------|---------|
| 路径不存在 | `Error: Path not found: {path}` |
| 无效正则 | `Error: invalid regex pattern: {e}` |
| 无匹配 | `No files found`（FindFiles）/ `No matches found`（Grep） |
| PermissionError | `Error: {exc}` |
| 其他 | `Error finding files: {exc}` / `Error searching: {exc}` |

## 5. 安全边界

- **路径越界**：复用 `_FsTool._resolve`
- **只读操作**：两个工具均 `read_only=True`
- **大文件保护**：GrepTool 跳过 >2MB 文件
- **二进制保护**：GrepTool 跳过二进制文件
- **噪声目录过滤**：复用 `ListDirTool._IGNORE_DIRS`

## 6. 测试策略

### `tests/test_search.py`
**FindFilesTool：**
1. `test_find_by_query`：按路径片段查找
2. `test_find_by_glob`：按 glob 模式过滤
3. `test_find_by_type`：按文件类型简写过滤
4. `test_find_ignore_dirs`：噪声目录被过滤
5. `test_find_head_limit`：截断提示
6. `test_find_no_results`：无匹配返回提示
7. `test_find_path_not_found`：路径不存在错误

**GrepTool：**
8. `test_grep_content_mode`：content 模式返回匹配行+行号
9. `test_grep_files_with_matches`：files_with_matches 模式返回文件路径
10. `test_grep_case_insensitive`：不区分大小写
11. `test_grep_fixed_strings`：固定字符串（非正则）
12. `test_grep_glob_filter`：glob 过滤
13. `test_grep_skip_binary`：二进制文件被跳过
14. `test_grep_invalid_regex`：无效正则返回错误
15. `test_grep_no_matches`：无匹配返回提示

**共享：**
16. `test_tools_discovered`：两个工具都被 ToolLoader 发现
