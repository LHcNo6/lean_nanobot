# step99：archive 系统提示模板化 + LLM 调用对齐

## 解决的问题

Consolidator.archive 使用简单硬编码提示，缺少 SNIP 分类标准；LLM 调用缺少 temperature、tools 等参数，不检查 finish_reason。

## 实现

1. `_CONSOLIDATOR_SYSTEM_PROMPT` 替换为参考实现的 SNIP 分类模板全文
2. LLM 调用添加 `temperature=getattr(runtime, "temperature", 0.7)`、`max_tokens=runtime.max_tokens`、`tools=None`、`tool_choice=None`
3. 检查 `response.finish_reason == "error"` 时 raise，触发 raw_archive 回退

## 文件修改清单

| 文件 | 操作 |
|------|------|
| `consolidation.py` | 修改：系统提示 + LLM 调用参数 + finish_reason 检查 |
| `tests/test_consolidator_archive.py` | 新建（9 测试） |
| 规范文档 + step99.md | 新建 |

## 测试结果

9 passed in 0.21s

## 下一步

**step100**：maybe_consolidate_by_tokens 改用 estimate_session_prompt_tokens + __init__ 新增 unified_session + _locks 改用 WeakValueDictionary。
