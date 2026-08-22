# Step 98 API 契约

## helpers.py 新增

### truncate_text_to_tokens
```python
def truncate_text_to_tokens(text: str, max_tokens: int) -> str
```
- max_tokens <= 0 → 返回原文
- tiktoken 可用 → 精确按 token 截断 + 后缀
- tiktoken 不可用 → 回退到 4 chars/token 估算

## consolidation.py 变更

### _truncate_to_token_budget
- 改用 `truncate_text_to_tokens(text, budget)` 替代 `truncate_text(text, budget * 4)`
