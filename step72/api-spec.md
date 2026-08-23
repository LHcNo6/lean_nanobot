# Step 72 API Specification

## 1. WebSearchTool API

**文件**：`tools/web.py`
**继承**：`Tool`

| 属性 | 值 |
|------|-----|
| `name` | `"web_search"` |
| `config_key` | `"web"` |
| `_scopes` | `{"core", "subagent"}` |
| `read_only` | `True` |

### 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `query` | string | 是 | — | 搜索关键词 |
| `count` | integer | 否 | `5` | 返回结果数（1-10） |

### 返回值

成功时返回文本：
```
Results for: {query}

1. {title}
   {url}
   {snippet}
```

无结果：`No results for: {query}`
失败：`ToolResult.error(...)`

## 2. SearchProvider 抽象基类

```python
class SearchProvider(ABC):
    @abstractmethod
    async def search(self, query: str, n: int) -> list[dict]
```
返回 `[{"title": str, "url": str, "content": str}, ...]`

## 3. DuckDuckGoProvider

- 搜索 URL: `https://html.duckduckgo.com/html/?q={query}`
- 解析 `result__a`（标题+URL）和 `result__snippet`（摘要）
- 从 `uddg` 参数提取真实 URL

## 4. 配置契约

`WebSearchConfig`：

| 字段 | 类型 | 默认值 |
|------|------|--------|
| `provider` | str | `"duckduckgo"` |
| `max_results` | int | `5` |
| `timeout` | int | `30` |

`WebToolsConfig.search: WebSearchConfig`

## 5. 工具发现契约

`ToolLoader` 扫描 `tools/web.py` 时发现 `WebFetchTool` 和 `WebSearchTool`。
最终注册：`web_fetch`、`web_search`
