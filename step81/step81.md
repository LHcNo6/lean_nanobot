# step81：WebSearch 多 provider 支持

## 实现

修改 `tools/web.py`：
- 新增 BraveProvider（Brave Search API，需 API key）
- 新增 TavilyProvider（Tavily API，需 API key）
- 新增 SearxngProvider（自建 SearXNG，需 base_url）
- _create_provider 工厂支持 4 种 provider
- WebSearchTool 新增 api_key/base_url 字段
- WebSearchConfig 新增 api_key/base_url 字段

## 文件修改清单

| 文件 | 操作 |
|------|------|
| `config/schema.py` | 修改：WebSearchConfig +api_key +base_url |
| `tools/web.py` | 修改：+3个Provider +工厂增强 +WebSearchTool新字段 |
| `tests/test_web_search_multi.py` | 新建（16测试） |
| `proposal.md`/`design.md`/`api-spec.md` | 新建 |

## 测试结果

16 passed
