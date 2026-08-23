# Step 80 Design: WebFetch 增强

## 1. 架构

```
tools/web.py（修改）
  ├── +_extract_readability(html)  启发式正文提取
  ├── +_fetch_jina(url)            Jina Reader API 调用
  └── WebFetchTool                 新增 mode 参数
```

## 2. 参数

```python
url: str          # 必填
mode: str = "auto"  # auto / readability / jina
max_chars: int = 60000
```

## 3. 三种模式

- **auto**：原有的 HTML 转纯文本（_strip_tags + _normalize）
- **readability**：先去除噪声标签（nav/footer/script/style/header/aside），再转纯文本
- **jina**：请求 https://r.jina.ai/{url}，返回 Markdown 格式正文

## 4. readability 启发式

用正则去除以下标签及其内容：
- `<script>...</script>`
- `<style>...</style>`
- `<nav>...</nav>`
- `<footer>...</footer>`
- `<header>...</header>`
- `<aside>...</aside>`
- 注释 `<!-- ... -->`

然后用原有的 _strip_tags 提取文本。

## 5. 测试策略

- readability 去除 script/style 内容
- readability 保留正文
- mode 参数校验
- 默认 mode=auto 行为不变
- 工具发现
