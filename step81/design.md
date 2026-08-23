# Step 81 Design: WebSearch 多 provider

## 1. 架构

```
tools/web.py（修改）
  ├── SearchProvider(ABC)          已有
  ├── DuckDuckGoProvider           已有
  ├── +BraveProvider               Brave Search API
  ├── +TavilyProvider              Tavily API
  ├── +SearxngProvider             自建 SearXNG
  └── +_create_provider(name, config)  增强工厂
```

## 2. Provider 实现

### BraveProvider
- API: https://api.search.brave.com/res/v1/web/search
- 需要 api_key（header: X-Subscription-Token）
- 解析 JSON 响应

### TavilyProvider
- API: https://api.tavily.com/search
- 需要 api_key（POST body）
- 解析 JSON 响应

### SearxngProvider
- API: {base_url}/search?q={query}&format=json
- 不需要 api_key
- 解析 JSON 响应

## 3. 配置增强

WebSearchConfig 新增：
- `api_key: str = ""`
- `base_url: str = ""`

## 4. 工厂逻辑

_create_provider(name, config=None):
- "duckduckgo" -> DuckDuckGoProvider()
- "brave" -> BraveProvider(api_key)
- "tavily" -> TavilyProvider(api_key)
- "searxng" -> SearxngProvider(base_url)
- 默认 -> DuckDuckGoProvider()

## 5. 测试策略

- _create_provider 创建各 provider
- BraveProvider 构造
- TavilyProvider 构造
- SearxngProvider 构造
- 未知 provider 回退到 duckduckgo
- WebSearchConfig 新字段
