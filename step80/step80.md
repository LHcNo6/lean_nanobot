# step80：WebFetch 增强

## 实现

修改 `tools/web.py` 中的 WebFetchTool：
- 新增 mode 参数：auto（默认）/ readability / jina
- readability：启发式正文提取（去除 nav/footer/script/style/header/aside）
- jina：Jina Reader API（https://r.jina.ai/{url}）
- 新增 _extract_readability 和 _fetch_jina 辅助函数

## 文件修改清单

| 文件 | 操作 |
|------|------|
| `tools/web.py` | 修改：+mode参数 +_extract_readability +_fetch_jina |
| `tests/test_web_fetch_enhanced.py` | 新建（10测试） |
| `proposal.md`/`design.md`/`api-spec.md` | 新建 |

## 测试结果

10 passed
