# Step 68 API Specification

## 1. 辅助函数 API

### 1.1 `_is_binary(raw)`

| 项目 | 说明 |
|------|------|
| 签名 | `(raw: bytes) -> bool` |
| 功能 | 检测字节内容是否为二进制 |
| 规则 | 含 null 字节 → True；前 4096 字节非文本控制字符比例 > 20% → True |

### 1.2 `_match_glob(rel_path, name, pattern)`

| 项目 | 说明 |
|------|------|
| 签名 | `(rel_path: str, name: str, pattern: str) -> bool` |
| 功能 | glob 模式匹配 |
| 规则 | 模式含 `/` 或 `**` → 匹配完整相对路径；否则匹配文件名 |

### 1.3 `_matches_type(name, file_type)`

| 项目 | 说明 |
|------|------|
| 签名 | `(name: str, file_type: str \| None) -> bool` |
| 功能 | 文件类型简写匹配 |
| 支持类型 | py, js, ts, md, json, yaml, toml, html, css, sh（其他自动用 `*.{type}`） |

## 2. 基类 API

### 2.1 `_SearchTool`

**文件**：`tools/search.py`
**继承**：`_FsTool`

| 方法 | 签名 | 说明 |
|------|------|------|
| `_display_path(target, root)` | `(target: Path, root: Path) -> str` | 返回 workspace-relative 路径（正斜杠） |
| `_iter_files(root)` | `(root: Path) -> Iterable[Path]` | 递归遍历文件，跳过噪声目录 |

### 2.2 `_FsTool._display_workspace()`（新增）

**文件**：`tools/filesystem.py`

| 项目 | 说明 |
|------|------|
| 签名 | `(self) -> Path \| None` |
| 功能 | 返回当前 workspace 的 Path |

## 3. FindFilesTool API

**文件**：`tools/search.py`
**继承**：`_SearchTool`

| 属性 | 值 |
|------|-----|
| `name` | `"find_files"` |
| `read_only` | `True` |
| `_scopes` | `{"core", "subagent"}` |

### 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `path` | string | 否 | `"."` | 搜索根目录或文件 |
| `query` | string | 否 | — | 路径片段搜索（空白分隔词，不区分大小写） |
| `glob` | string | 否 | — | glob 过滤，如 `*.py` |
| `type` | string | 否 | — | 文件类型简写，如 `py`、`md` |
| `head_limit` | integer | 否 | `200` | 最大返回数（0=不限制） |

### 返回值
- 成功：每行一个 workspace-relative 文件路径
- 无匹配：`"No files found"`
- 截断：末尾追加 `(truncated, showing first {N} of {M} files)`
- 失败：`ToolResult.error(...)`

## 4. GrepTool API

**文件**：`tools/search.py`
**继承**：`_SearchTool`

| 属性 | 值 |
|------|-----|
| `name` | `"grep"` |
| `read_only` | `True` |
| `_scopes` | `{"core", "subagent"}` |
| `_MAX_FILE_BYTES` | `2_000_000` | 跳过 >2MB 的文件 |

### 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `pattern` | string | 是 | — | 正则或纯文本模式 |
| `path` | string | 否 | `"."` | 搜索根目录或文件 |
| `glob` | string | 否 | — | glob 过滤 |
| `type` | string | 否 | — | 文件类型简写 |
| `case_insensitive` | boolean | 否 | `false` | 不区分大小写 |
| `fixed_strings` | boolean | 否 | `false` | 纯文本模式（非正则） |
| `output_mode` | string | 否 | `"files_with_matches"` | `"content"` 或 `"files_with_matches"` |
| `head_limit` | integer | 否 | `250` | 最大返回数（0=不限制） |

### 返回值
- content 模式：每行 `"{path}:{line_no}| {content}"`
- files_with_matches 模式：每行一个匹配文件路径
- 无匹配：`"No matches found"`
- 截断：末尾追加提示
- 失败：`ToolResult.error(...)`

## 5. 工具发现契约

`ToolLoader` 扫描 `tools/search.py` 时：
- `FindFilesTool`、`GrepTool` 是具体 Tool 子类 → 被发现
- `_SearchTool` 以下划线开头 → 被过滤
- 辅助函数 → 被过滤

最终注册的工具名：`find_files`、`grep`
