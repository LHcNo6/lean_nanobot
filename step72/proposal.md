# Step 72 Proposal: WebSearchTool 网页搜索

## 1. 问题背景

agent 无法搜索网页。需要 WebSearchTool 执行关键词搜索，返回标题、URL、摘要。

nanobot 支持 12 个搜索 provider（duckduckgo/brave/tavily 等），使用 ddgs 包。
step72 以最小增量实现：provider 抽象 + DuckDuckGo HTML 搜索（标准库，无新依赖）。

## 2. 目标

在 `tools/web.py` 中新增：
1. `SearchProvider` 抽象基类（search(query, n) -> list[dict]）
2. `DuckDuckGoProvider`（HTML 搜索页面 + 正则解析）
3. `WebSearchTool`（调用 provider，格式化结果）
4. 配置：WebSearchConfig（provider, max_results, timeout）

## 3. 非目标

- 不实现其他 provider（brave/tavily 等需要 API key）
- 不使用 ddgs 包（避免新依赖）
- 不实现时间范围过滤
- 不实现高级搜索参数

## 4. 验收标准

1. WebSearchTool 可被 ToolLoader 发现
2. DuckDuckGoProvider 能解析 HTML 搜索结果
3. 搜索结果格式化为编号列表（标题/URL/摘要）
4. 无结果时返回提示
5. 配置 provider 切换预留接口
6. 单元测试通过（mock HTTP 请求）
