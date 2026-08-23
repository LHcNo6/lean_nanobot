# Step 76 API Specification

## 1. ReadFileTool API（升级版）

**文件**：`tools/filesystem.py`
**继承**：`_FsTool`

| 属性 | 值 |
|------|-----|
| `name` | `"read_file"` |
| `_scopes` | `{"core", "subagent"}` |
| `read_only` | `True` |
| `_MAX_CHARS` | `128000` |

### 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `path` | string | 是 | — | 文件路径 |
| `offset` | integer | 否 | `1` | 起始行号（1-based） |
| `limit` | integer | 否 | `None` | 返回行数（None=不限制） |
| `max_chars` | integer | 否 | `60000` | 最大字符数 |

### 输出格式

```
1|第一行
2|第二行
3|第三行
```

### 返回值

成功：文本（每行 `N|content`）
空文件：`"(Empty file: {path})"`
失败：`ToolResult.error(...)`

## 2. 工具发现契约

ReadFileTool 从 `tools/filesystem.py` 发现，旧的 `tools/read_file.py` 被删除。
最终注册：`read_file`
