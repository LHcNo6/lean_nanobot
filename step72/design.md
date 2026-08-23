# Step 72 Design: WebSearchTool

## 1. 架构

```
tools/web.py（扩展）
  ├── SearchProvider（ABC）       搜索 provider 抽象基类
  ├── DuckDuckGoProvider          DuckDuckGo HTML 搜索实现
  ├── _format_search_results()    搜索结果格式化
  └── WebSearchTool(Tool)         网页搜索工具
```

## 2. 配置

`WebToolsConfig` 新增 `search` 字段：
```python
class WebSearchConfig(Base):
    provider: str = "duckduckgo"
    max_results: int = 5
    timeout: int = 30
```

## 3. SearchProvider 抽象基类

```python
class SearchProvider(ABC):
    @abstractmethod
    async def search(self, query: str, n: int) -> list[dict]:
        """返回 [{"title": ..., "url": ..., "content": ...}, ...]"""
```

## 4. DuckDuckGoProvider

- URL: https://html.duckduckgo.com/html/?q={query}
- 用 urllib + asyncio.to_thread 获取
- 正则解析 <a class="result__a">（标题+URL）和 <a class="result__snippet">（摘要）
- URL 需要从 duckduckgo.com/l/?uddg= 参数中提取真实 URL

## 5. WebSearchTool 执行流程

1. 参数校验（query 必填）
2. 根据 config.provider 创建 provider
3. 调用 provider.search(query, n)
4. 格式化结果（编号列表：标题/URL/摘要）
5. 返回文本

## 6. 结果格式

```
Results for: {query}

1. {title}
   {url}
   {snippet}

2. ...
```

无结果：`No results for: {query}`

## 7. 测试策略

mock urllib.request.urlopen，返回模拟的 DuckDuckGo HTML 搜索结果页面。
