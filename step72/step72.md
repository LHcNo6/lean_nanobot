# step72：WebSearchTool 网页搜索

## 1. 实现

在 `tools/web.py` 中新增：
- `SearchProvider` 抽象基类
- `DuckDuckGoProvider`（HTML 搜索页面 + 正则解析，无需 API key）
- `_create_provider` 工厂函数
- `_format_search_results` 结果格式化
- `WebSearchTool` 工具类

配置：`WebSearchConfig`（provider, max_results, timeout），`WebToolsConfig.search`

## 2. 文件修改清单

| 文件 | 操作 |
|------|------|
| `config/schema.py` | 修改：+WebSearchConfig + WebToolsConfig.search |
| `tools/web.py` | 修改：+SearchProvider + DuckDuckGoProvider + WebSearchTool |
| `tests/test_web_search.py` | 新建：23 个测试 |
| `proposal.md`/`design.md`/`api-spec.md` | 新建 |

## 3. 测试结果

47 passed（web_search 23 + web_fetch 24，无回归）

## 4. 技术债

- 仅 DuckDuckGo provider，其他 provider（brave/tavily 等）未实现
- 无时间范围过滤
- 无高级搜索参数
- DuckDuckGo HTML 解析可能随页面结构变化而失效
