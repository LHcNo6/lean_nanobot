# Step 77 API Specification

## 1. GlobTool API

**文件**：`tools/glob_tool.py`
**继承**：`_FsTool`

| 属性 | 值 |
|------|-----|
| `name` | `"glob"` |
| `_scopes` | `{"core"}` |
| `read_only` | `True` |

### 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `pattern` | string | 是 | — | glob 模式（`*`, `?`, `**`, `[seq]`） |
| `path` | string | 否 | `"."` | 搜索起始路径 |
| `max_results` | integer | 否 | `200` | 最大结果数 |

### 返回值

成功：文本（文件列表）
失败：`ToolResult.error(...)`

## 2. 工具发现契约

`ToolLoader` 扫描 `tools/glob_tool.py` 时发现 `GlobTool`。
最终注册：`glob`
