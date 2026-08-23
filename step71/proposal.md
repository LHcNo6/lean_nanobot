# Step 71 Proposal: WebFetchTool 网页抓取

## 1. 问题背景

agent 无法访问网页内容。需要 WebFetchTool 抓取 URL 并提取可读文本，
支持 agent 进行网页内容分析、文档阅读等任务。

nanobot 的 WebFetchTool（web.py）使用 httpx + Jina Reader + readability，
功能完整但依赖重。step71 以最小增量实现基础版，用标准库 urllib。

## 2. 目标

新建 `tools/web.py`，实现 `WebFetchTool`：
1. 参数：url（必填），max_chars（可选，默认 50000）
2. 使用 `urllib.request` + `asyncio.to_thread` 异步获取
3. URL 验证（仅 http/https）
4. 超时控制（默认 30s）
5. HTML 转纯文本（去除 script/style 标签，去 HTML 标签，解码实体）
6. 输出截断
7. 外部内容横幅标记
8. 配置集成：WebToolsConfig（enable, timeout, user_agent）

## 3. 非目标

- 不使用 httpx（避免新依赖）
- 不实现 Jina Reader API
- 不实现 readability 正文提取
- 不实现 SSRF 保护（IP 解析检查）
- 不实现图片检测和返回
- 不实现代理支持
- 不实现流式响应
- 不实现 extract_mode（markdown/text 选择）

## 4. 验收标准

1. WebFetchTool 可被 ToolLoader 自动发现
2. 有效 URL 抓取成功，返回纯文本内容
3. 无效 scheme（ftp/file）被拒绝
4. 超时返回错误
5. HTML 标签被去除，实体被解码
6. 长内容被截断
7. 输出包含外部内容横幅
8. config.web.enable=False 时不加载
9. 单元测试通过（用 mock 避免真实网络请求）
