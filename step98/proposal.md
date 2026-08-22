# Step 98 Proposal: truncate_text_to_tokens + 截断预算对齐

## 1. 问题背景

Consolidator 的 `_truncate_to_token_budget` 当前用 `chars = budget * 4` 粗估后调用 `truncate_text`，这对中文/CJK 内容不准确（中文字符通常 1-2 token/字符，而非 4 字符/token）。参考实现使用 `truncate_text_to_tokens`，基于 tiktoken 精确计算 token 数，tiktoken 不可用时回退到 char 估算。

## 2. 目标

1. helpers.py 新增 `truncate_text_to_tokens(text, max_tokens)`：优先用 tiktoken cl100k_base 精确截断，不可用时回退到 4 chars/token 估算
2. consolidation.py `_truncate_to_token_budget` 改用 `truncate_text_to_tokens`

## 3. 非目标

- 不修改 `_input_token_budget`（当前 runtime.max_tokens 已通过 property 对齐）
- 不修改 archive 方法的 LLM 调用（step99）

## 4. 验收标准

1. tiktoken 可用时精确按 token 截断
2. tiktoken 不可用时回退到 char 估算
3. max_tokens <= 0 时返回原文
4. 截断后带 "... (truncated)" 后缀
5. 现有测试全部通过
