# Step 67 Design: ListDirTool

## 1. 架构概览

```
tools/filesystem.py
  ├── _FsTool(Tool)              [step65]
  ├── WriteFileTool(_FsTool)     [step65]
  ├── _MatchSpan / _find_matches [step66]
  ├── EditFileTool(_FsTool)      [step66]
  └── ListDirTool(_FsTool)       [step67 新增]
```

## 2. 模块详细设计

### 2.1 ListDirTool

#### 类属性

```python
class ListDirTool(_FsTool):
    _scopes = {"core", "subagent"}
    _DEFAULT_MAX = 200
    _IGNORE_DIRS = {
        ".git", "node_modules", "__pycache__", ".venv", "venv",
        "dist", "build", ".tox", ".mypy_cache", ".pytest_cache",
        ".ruff_cache", ".coverage", "htmlcov",
    }
```

#### 参数 Schema

```python
@tool_parameters(tool_parameters_schema(
    path=StringSchema("The directory path to list"),
    recursive=BooleanSchema(description="Recursively list all files (default false)"),
    max_entries=IntegerSchema(
        "Maximum entries to return (default 200)",
        minimum=1,
    ),
    required=["path"],
))
```

#### execute 流程

```
1. 参数校验
   └─ path 为 None/空 → error

2. 路径解析：dp = self._resolve(path)  （_resolve = _resolve_read，只读操作）

3. 存在性检查
   ├─ 不存在 → error("Directory not found")
   └─ 不是目录 → error("Not a directory")

4. 确定截断上限：cap = max_entries or _DEFAULT_MAX

5. 遍历目录
   ├─ 非递归：
   │   for item in sorted(dp.iterdir()):
   │     if item.name in _IGNORE_DIRS: skip
   │     total += 1
   │     if len(items) < cap:
   │       items.append(f"{item.name}/" if item.is_dir() else item.name)
   │
   └─ 递归：
       for item in sorted(dp.rglob("*")):
         if any(p in _IGNORE_DIRS for p in item.parts): skip
         total += 1
         if len(items) < cap:
           rel = item.relative_to(dp)
           items.append(f"{rel}/" if item.is_dir() else str(rel))

6. 结果组装
   ├─ 空目录（total == 0）→ "Directory {path} is empty"
   ├─ result = "\n".join(items)
   └─ total > cap → result += f"\n\n(truncated, showing first {cap} of {total} entries)"

7. 返回 result
```

#### 关键方法

| 方法 | 说明 |
|------|------|
| `execute(path, recursive, max_entries, **kwargs)` | 主执行方法 |
| `name` (property) | 返回 `"list_dir"` |
| `description` (property) | 工具描述 |
| `read_only` (property) | 返回 `True` |

## 3. 输出格式示例

### 非递归
```
src/
tests/
README.md
pyproject.toml
```

### 递归
```
src/
src/agent.py
src/utils.py
tests/
tests/test_agent.py
README.md
```

### 截断
```
file1.py
file2.py
...
(truncated, showing first 200 of 500 entries)
```

## 4. 错误处理

| 场景 | 返回消息 |
|------|---------|
| 空 path | `Error: list_dir requires a 'path' parameter.` |
| 目录不存在 | `Error: Directory not found: {path}` |
| 不是目录 | `Error: Not a directory: {path}` |
| PermissionError | `Error: {exc}` |
| 其他 OSError | `Error listing directory: {exc}` |

## 5. 安全边界

- **路径越界**：复用 `_FsTool._resolve`（即 `_resolve_read`），受限模式下强制路径在 workspace 内
- **只读操作**：`read_only=True`，无副作用
- **噪声目录过滤**：自动跳过 `.git`、`node_modules` 等，避免输出过大和敏感信息泄漏

## 6. 测试策略

### 单元测试 `tests/test_list_dir.py`
1. `test_list_non_recursive`：非递归列出直接子项
2. `test_list_recursive`：递归遍历所有子项
3. `test_ignore_dirs`：噪声目录被过滤
4. `test_max_entries_truncation`：超过 max_entries 截断并提示
5. `test_empty_directory`：空目录返回空目录消息
6. `test_directory_not_found`：目录不存在返回错误
7. `test_not_a_directory`：路径是文件返回错误
8. `test_dir_suffix`：目录项带 `/` 后缀
9. `test_read_only`：read_only=True
10. `test_tool_discovered_by_loader`：ToolLoader 自动发现
11. `test_tool_schema`：参数 schema 正确
12. `test_custom_max_entries`：自定义 max_entries 生效
