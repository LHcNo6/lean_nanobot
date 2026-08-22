# step100：token 估算对齐 + unified_session + WeakValueDictionary

## 解决的问题

Consolidator 用 `sum(estimate_message_tokens)` 逐个估算，未考虑系统提示/工具开销；_locks 普通 dict 无限增长；缺少 unified_session 参数。

## 实现

1. `__init__` 新增 `unified_session: bool = False`
2. `_locks` 改用 `weakref.WeakValueDictionary`，`get_lock` 改用 `setdefault`
3. `maybe_consolidate_by_tokens` 两处估算改用 `estimate_session_prompt_tokens`（该方法已存在，返回 (tokens, source)）

## 文件修改清单

| 文件 | 操作 |
|------|------|
| `consolidation.py` | 修改：+weakref 导入 +unified_session +WeakValueDictionary +两处估算改用 |
| `tests/test_consolidator_tokens.py` | 新建（9 测试） |
| 规范文档 + step100.md | 新建 |

## 测试结果

9 passed in 0.23s

## 下一步

**step101**：新增 `utils/workspace_prompts.py` 模块 + MemoryStore dream 模板方法（dream_prompt_file/has_dream_prompt_override/default_dream_prompt/_dream_template）。
