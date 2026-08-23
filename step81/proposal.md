# Step 81 Proposal: WebSearch 多 provider 支持

## 1. 问题背景

当前 WebSearchTool 只支持 DuckDuckGo HTML 解析，没有 API key 的 provider 选项。
nanobot 支持 brave/tavily/searxng 等多个 provider。

## 2. 目标

增强 `tools/web.py` 中的 WebSearchTool：
1. 新增 BraveProvider（Brave Search API，需 API key）
2. 新增 TavilyProvider（Tavily API，需 API key）
3. 新增 SearxngProvider（自建 SearXNG 实例）
4. 修改 _create_provider 工厂支持更多 provider
5. WebSearchConfig 添加 api_key 和 base_url 字段

## 3. 非目标

- 不实现真实的 API 调用测试（用 mock）
- 不实现 provider 自动降级/回退

## 4. 验收标准

1. BraveProvider/TavilyProvider/SearxngProvider 类存在
2. _create_provider 能根据名称创建对应 provider
3. WebSearchConfig 有 api_key 和 base_url 字段
4. 单元测试通过
