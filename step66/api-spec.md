# Step 66 API Specification

## 1. 工具层 API

### 1.1 `_MatchSpan`（dataclass）

**文件**：`tools/filesystem.py`

| 字段 | 类型 | 说明 |
|------|------|------|
| `start` | `int` | 在 content 中的起始偏移（0-indexed） |
| `end` | `int` | 结束偏移（exclusive） |
| `text` | `str` | 匹配到的实际文本 |
| `line` | `int` | 起始行号（1-indexed，用于错误提示） |

### 1.2 `_find_matches(content, old_text)`

**文件**：`tools/filesystem.py`

| 项目 | 说明 |
|------|------|
| 签名 | `(content: str, old_text: str) -> list[_MatchSpan]` |
| 功能 | 在 content 中查找所有 old_text 的精确匹配 |
| 返回 | 匹配位置列表，按出现顺序排列 |
| 空 old_text | 返回空列表（`str.find("", start)` 会匹配每个位置，通过 `max(1, len(old_text))` 步进避免无限循环，但空串无意义） |

### 1.3 `EditFileTool`

**文件**：`tools/filesystem.py`

**继承**：`_FsTool`

#### 类属性

| 属性 | 值 | 说明 |
|------|-----|------|
| `_scopes` | `{"core", "subagent", "memory"}` | 适用范围 |

#### 实例属性（property）

| 属性 | 类型 | 值 | 说明 |
|------|------|-----|------|
| `name` | `str` | `"edit_file"` | 工具名 |
| `description` | `str` | （见下） | 工具描述 |
| `read_only` | `bool` | `False` | 有副作用 |

`description` 全文：
> Perform a small, exact replacement in one file by replacing old_text with new_text.
> Use this for narrow text substitutions with old_text copied from read_file.
> If old_text matches multiple times, provide more context or set occurrence or replace_all=true.

#### 参数 Schema

通过 `@tool_parameters` 装饰器定义：

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `path` | `string` | 是 | — | 要编辑的文件路径（绝对或 workspace 相对） |
| `old_text` | `string` | 是 | — | 要替换的精确文本（从 read_file 输出复制） |
| `new_text` | `string` | 是 | — | 替换文本 |
| `replace_all` | `boolean` | 否 | `false` | 是否替换所有匹配 |
| `occurrence` | `integer` | 否 | — | 替换第 N 个匹配（1-indexed），不能与 replace_all 同用 |

#### 方法

| 方法 | 签名 | 说明 |
|------|------|------|
| `execute(path, old_text, new_text, replace_all, occurrence, **kwargs)` | `async (...) -> ToolResult` | 执行编辑 |

**返回值**：
- 成功：`ToolResult("Successfully edited {path}")`，如有 read-before-edit 警告则前缀 `"{warning}\n"`
- 歧义警告（不执行替换）：普通字符串（非 ToolResult.error），内容为 `"Warning: old_text appears {N} times at line {n1}, ..."`
- 失败：`ToolResult.error("Error: ...")`

**执行逻辑**：
1. 参数校验（path/old_text/new_text 非空，occurrence >= 1，replace_all 与 occurrence 互斥）
2. `_resolve_write(path)` 解析路径
3. 文件不存在 → 错误
4. 读取文件，检测 CRLF，统一转 LF
5. `_file_states.check_read(fp)` 获取警告
6. `_find_matches(content, old_text)` 查找所有匹配
7. 无匹配 → 错误
8. 选择匹配（replace_all / occurrence / 单匹配 / 多匹配歧义）
9. 倒序替换
10. CRLF 还原，写回文件
11. `_file_states.record_write(fp)`
12. 返回结果（含警告）

**副作用**：
1. 修改文件内容
2. 更新 FileStates（record_write）

## 2. 工具发现契约

`ToolLoader` 扫描 `tools/filesystem.py` 时：
- `EditFileTool` 是 `Tool` 的具体子类 → 被发现
- `_MatchSpan` 不是 `Tool` 子类 → 被过滤
- `_find_matches` 是函数 → 被过滤
- `_FsTool` 以下划线开头 → 被 `not attr_name.startswith("_")` 过滤

最终注册的工具名：`edit_file`

## 3. 与 step65 的关系

- 复用 `_FsTool` 基类的 `_resolve_write`、`_file_states`、`create`
- 复用 `FileStates.check_read` 实现 read-before-edit
- 复用 `FileStates.record_write` 更新文件状态
- 不修改 step65 的任何现有代码，纯增量
