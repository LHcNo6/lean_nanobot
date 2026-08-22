# step98：truncate_text_to_tokens + 截断预算对齐

## 解决的问题

Consolidator 的 `_truncate_to_token_budget` 用 `chars = budget * 4` 粗估，对中文/CJK 内容不准确。

## 实现

1. helpers.py 新增 `_get_token_encoding()`（tiktoken cl100k_base）和 `truncate_text_to_tokens(text, max_tokens)`
2. tiktoken 可用时精确按 token 截断（含后缀预算计算）；不可用时回退到 4 chars/token
3. consolidation.py `_truncate_to_token_budget` 改用 `truncate_text_to_tokens`

## 文件修改清单

| 文件 | 操作 |
|------|------|
| `helpers.py` | 修改：+_get_token_encoding +truncate_text_to_tokens |
| `consolidation.py` | 修改：+导入 +_truncate_to_token_budget 改用新函数 |
| `tests/test_truncate_tokens.py` | 新建（7 测试） |
| 规范文档 + step98.md | 新建 |

## 测试结果

7 passed in 0.12s

## 下一步

**step99**：Consolidator.archive 系统提示模板化 + LLM 调用参数对齐（temperature/reasoning_effort/finish_reason 检查）。
