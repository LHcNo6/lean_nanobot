# Step 80 API Specification

## 1. WebFetchTool API（增强版）

**文件**：`tools/web.py`
**继承**：`Tool`

| 属性 | 值 |
|------|-----|
| `name` | `"web_fetch"` |
| `_scopes` | `{"core"}` |

### 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `url` | string | 是 | — | 要获取的 URL |
| `mode` | string | 否 | `"auto"` | 提取模式：auto/readability/jina |
| `max_chars` | integer | 否 | `60000` | 最大字符数 |

### 模式说明

- `auto`：HTML 转纯文本（默认）
- `readability`：启发式正文提取（去除 nav/footer/script 等）
- `jina`：Jina Reader API（https://r.jina.ai/{url}）

### 返回值

成功：文本（网页内容）
失败：`ToolResult.error(...)`

## 2. 工具发现契约

WebFetchTool 仍从 `tools/web.py` 发现，工具名仍为 `web_fetch`。
