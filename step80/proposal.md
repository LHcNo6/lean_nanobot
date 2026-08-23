# Step 80 Proposal: WebFetch 增强

## 1. 问题背景

当前 WebFetchTool 只支持简单的 HTML 转纯文本，输出包含大量导航、页脚等噪声。
nanobot 支持 readability 正文提取和 Jina Reader API。

## 2. 目标

增强 `tools/web.py` 中的 WebFetchTool：
1. 新增 mode 参数：auto（默认）/ readability / jina
2. readability：启发式正文提取（去除 nav/footer/script/style）
3. jina：通过 Jina Reader API（https://r.jina.ai/{url}）获取纯净正文
4. 保留原有的 HTML 转纯文本作为 auto 模式

## 3. 非目标

- 不实现完整的 readability 算法（用简化启发式）
- 不依赖第三方库（beautifulsoup4/readability-lxml）

## 4. 验收标准

1. mode=readability 去除噪声标签
2. mode=jina 调用 Jina Reader API
3. 默认 mode=auto 行为不变
4. 单元测试通过
