# Step 71 Design: WebFetchTool

## 1. 架构

```
tools/web.py（新建）
  ├── WebToolsConfig        配置（enable, timeout, user_agent）
  ├── _strip_tags(text)     HTML 标签去除 + 实体解码
  ├── _normalize(text)      空白规范化
  ├── _validate_url(url)    URL 验证（http/https + 域名）
  └── WebFetchTool(Tool)    网页抓取工具
```

## 2. 配置

`config/schema.py` 新增 `WebToolsConfig`：
```python
class WebToolsConfig(Base):
    enable: bool = True
    timeout: int = 30
    user_agent: str = "Mozilla/5.0 (learn_nano)"
```
`Config` 添加 `web: WebToolsConfig` 字段。

## 3. WebFetchTool 执行流程

1. URL 清洗（去除首尾空白和引号）
2. URL 验证（http/https scheme + 域名）
3. 构建请求（User-Agent header）
4. `asyncio.to_thread` 中执行 `urllib.request.urlopen`（带超时）
5. 读取响应内容，检测编码
6. HTML 转纯文本（去 script/style → 去标签 → 解码实体 → 规范化空白）
7. 添加外部内容横幅
8. 输出截断（max_chars）
9. 返回 JSON 格式结果（url, status, length, truncated, content）

## 4. HTML 转纯文本

```python
def _strip_tags(text: str) -> str:
    text = re.sub(r'<script[\s\S]*?</script>', '', text, flags=re.I)
    text = re.sub(r'<style[\s\S]*?</style>', '', text, flags=re.I)
    text = re.sub(r'<[^>]+>', '', text)
    return html.unescape(text).strip()

def _normalize(text: str) -> str:
    text = re.sub(r'[ \t]+', ' ', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()
```

## 5. 返回格式

```json
{
  "url": "https://example.com",
  "status": 200,
  "length": 1234,
  "truncated": false,
  "content": "[External content]\n\nPage title..."
}
```

错误时返回 `ToolResult.error(...)`。

## 6. 测试策略

使用 `unittest.mock.patch` 模拟 `urllib.request.urlopen`，避免真实网络请求。
- 测试 HTML 转纯文本
- 测试 URL 验证
- 测试超时
- 测试输出截断
- 测试工具发现和配置
