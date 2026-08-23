# step71：WebFetchTool 网页抓取

## 1. 实现

新建 `tools/web.py`，实现 WebFetchTool：
- URL 验证（http/https）
- urllib.request + asyncio.to_thread 异步获取
- HTML 转纯文本（去 script/style → 去标签 → 解码实体 → 规范化空白）
- 输出截断（默认 50000 字符）
- 外部内容横幅标记
- 配置：WebToolsConfig（enable, timeout, user_agent）

## 2. 文件修改清单

| 文件 | 操作 |
|------|------|
| `config/schema.py` | 修改：+WebToolsConfig + ToolsConfig.web |
| `tools/web.py` | 新建：WebFetchTool + 辅助函数 |
| `tests/test_web_fetch.py` | 新建：24 个测试 |
| `proposal.md`/`design.md`/`api-spec.md` | 新建 |

## 3. 测试结果

24 passed（辅助函数 8 + WebFetchTool 9 + 发现配置 7）

## 4. 技术债

- 无 Jina Reader / readability 正文提取
- 无 SSRF 保护（IP 解析检查）
- 无图片检测和返回
- 无代理支持
- 无 extract_mode 选择
