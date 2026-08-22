# Step 98 Design: truncate_text_to_tokens + 截断预算对齐

## 原理

tiktoken 的 cl100k_base 编码是 OpenAI 模型的标准分词器。精确截断需要：
1. 编码全文为 token
2. 计算后缀 token 数，body_budget = max_tokens - suffix_tokens
3. 从 body_budget 往下找最大的 candidate，使得 decode(candidate) + suffix 的总 token <= max_tokens
4. tiktoken 不可用时回退到 max_chars = max_tokens * 4

## 文件修改清单

| 文件 | 操作 |
|------|------|
| `helpers.py` | 修改：+_get_token_encoding +truncate_text_to_tokens |
| `consolidation.py` | 修改：_truncate_to_token_budget 改用新函数 |
| `tests/test_truncate_tokens.py` | 新建 |
| 规范文档 + step98.md | 新建 |
