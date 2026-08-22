# Step 99 API 契约

## consolidation.py 变更

### _CONSOLIDATOR_SYSTEM_PROMPT（模块级常量）
替换为参考实现的 SNIP 分类模板全文。

### archive（行为变更）
- LLM 调用添加 temperature、max_tokens=runtime.max_tokens、tools=None、tool_choice=None
- response.finish_reason == "error" 时 raise RuntimeError，触发 raw_archive 回退
