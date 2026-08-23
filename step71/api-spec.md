# Step 71 API Specification

## 1. WebFetchTool API

**文件**：`tools/web.py`
**继承**：`Tool`

| 属性 | 值 |
|------|-----|
| `name` | `"web_fetch"` |
| `config_key` | `"web"` |
| `_scopes` | `{"core", "subagent"}` |
| `read_only` | `True` |

### 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `url` | string | 是 | — | 要抓取的 URL（http/https） |
| `max_chars` | integer | 否 | `50000` | 最大返回字符数 |

### 返回值

成功时返回 JSON 字符串：
```json
{"url": "...", "status": 200, "length": 1234, "truncated": false, "content": "..."}
```

失败时返回 `ToolResult.error(...)`：
- 无效 URL：`"Error: Invalid URL: ..."`
- 超时：`"Error: Request timed out after {N} seconds"`
- HTTP 错误：`"Error: HTTP {status}: {reason}"`
- 网络错误：`"Error: Failed to fetch URL: {exc}"`

## 2. 辅助函数

### `_validate_url(url) -> tuple[bool, str]`
验证 URL scheme 和域名。仅允许 http/https。

### `_strip_tags(text) -> str`
去除 HTML 标签（先移除 script/style），解码 HTML 实体。

### `_normalize(text) -> str`
规范化空白（合并空格，限制连续空行）。

## 3. 配置契约

`config/schema.py` 新增 `WebToolsConfig`：

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `enable` | bool | `True` | 是否启用 web 工具 |
| `timeout` | int | `30` | 请求超时秒数 |
| `user_agent` | str | `"Mozilla/5.0 (learn_nano)"` | User-Agent |

`Config` 添加 `web: WebToolsConfig` 字段。

## 4. 工具发现契约

`ToolLoader` 扫描 `tools/web.py` 时：
- `WebFetchTool` 是具体 Tool 子类 → 被发现
- 辅助函数和配置类 → 被过滤

最终注册的工具名：`web_fetch`
