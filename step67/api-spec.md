# Step 67 API Specification

## 1. 工具层 API

### 1.1 `ListDirTool`

**文件**：`tools/filesystem.py`

**继承**：`_FsTool`

#### 类属性

| 属性 | 类型 | 值 | 说明 |
|------|------|-----|------|
| `_scopes` | `set[str]` | `{"core", "subagent"}` | 适用范围 |
| `_DEFAULT_MAX` | `int` | `200` | 默认最大返回条目数 |
| `_IGNORE_DIRS` | `set[str]` | （见下） | 自动过滤的噪声目录名 |

`_IGNORE_DIRS` 内容：
```python
{".git", "node_modules", "__pycache__", ".venv", "venv",
 "dist", "build", ".tox", ".mypy_cache", ".pytest_cache",
 ".ruff_cache", ".coverage", "htmlcov"}
```

#### 实例属性（property）

| 属性 | 类型 | 值 | 说明 |
|------|------|-----|------|
| `name` | `str` | `"list_dir"` | 工具名 |
| `description` | `str` | （见下） | 工具描述 |
| `read_only` | `bool` | `True` | 只读，无副作用 |

`description` 全文：
> List the contents of a directory. Set recursive=true to explore nested structure.
> Common noise directories (.git, node_modules, __pycache__, etc.) are auto-ignored.

#### 参数 Schema

通过 `@tool_parameters` 装饰器定义：

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `path` | `string` | 是 | — | 要列出的目录路径（绝对或 workspace 相对） |
| `recursive` | `boolean` | 否 | `false` | 是否递归遍历子目录 |
| `max_entries` | `integer` | 否 | `200` | 最大返回条目数（minimum=1） |

#### 方法

| 方法 | 签名 | 说明 |
|------|------|------|
| `execute(path, recursive, max_entries, **kwargs)` | `async (...) -> str \| ToolResult` | 执行目录列表 |

**返回值**：
- 成功：纯文本字符串，每行一个条目，目录带 `/` 后缀；截断时末尾追加提示
- 空目录：`"Directory {path} is empty"`
- 失败：`ToolResult.error("Error: ...")`

**执行逻辑**：
1. 参数校验（path 非空）
2. `self._resolve(path)` 解析路径（只读操作，用 `_resolve_read`）
3. 目录不存在/不是目录 → 错误
4. 确定截断上限 `cap = max_entries or _DEFAULT_MAX`
5. 遍历（非递归 `iterdir()` / 递归 `rglob("*")`），过滤噪声目录
6. 组装结果，截断提示
7. 返回

**副作用**：无（只读操作）

## 2. 工具发现契约

`ToolLoader` 扫描 `tools/filesystem.py` 时：
- `ListDirTool` 是 `Tool` 的具体子类 → 被发现
- `_IGNORE_DIRS` 是类属性 → 被过滤（非 Tool 子类）
- `_FsTool` 以下划线开头 → 被过滤

最终注册的工具名：`list_dir`

## 3. 与 step65-66 的关系

- 复用 `_FsTool` 基类的 `_resolve`（即 `_resolve_read`）路径解析
- 不修改任何现有代码，纯增量
- 与 `WriteFileTool`/`EditFileTool` 共享 `_FsTool` 基础设施
