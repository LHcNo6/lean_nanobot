# Step 81 API Specification

## 1. WebSearchTool API（增强版）

**文件**：`tools/web.py`
**继承**：`Tool`

| 属性 | 值 |
|------|-----|
| `name` | `"web_search"` |
| `_scopes` | `{"core"}` |

### 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `query` | string | 是 | — | 搜索关键词 |
| `count` | integer | 否 | `5` | 返回结果数（1-10） |

### 支持的 Provider

| Provider | 名称 | 需要 API key | 说明 |
|----------|------|-------------|------|
| DuckDuckGo | `duckduckgo` | 否 | HTML 解析（默认） |
| Brave | `brave` | 是 | Brave Search API |
| Tavily | `tavily` | 是 | Tavily Search API |
| SearXNG | `searxng` | 否 | 自建 SearXNG 实例 |

## 2. 配置增强

`WebSearchConfig` 新增：
- `api_key: str = ""`
- `base_url: str = ""`（SearXNG 实例地址）

## 3. 工具发现契约

WebSearchTool 仍从 `tools/web.py` 发现，工具名仍为 `web_search`。
