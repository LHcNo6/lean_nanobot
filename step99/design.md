# Step 99 Design: archive 系统提示模板化 + LLM 调用对齐

## 实现

1. `_CONSOLIDATOR_SYSTEM_PROMPT` 替换为参考实现的 SNIP 模板全文
2. archive 中 LLM 调用：
   - `temperature=getattr(runtime, 'temperature', 0.7)`（兼容 Runtime/LLMRuntime）
   - `max_tokens=runtime.max_tokens`
   - `tools=None, tool_choice=None`（如 provider.chat 支持）
3. response 检查：`getattr(response, 'finish_reason', None) == "error"` → raise → 触发 raw_archive

## 文件修改清单

| 文件 | 操作 |
|------|------|
| `consolidation.py` | 修改：系统提示 + LLM 调用参数 + finish_reason 检查 |
| `tests/test_consolidator_archive.py` | 新建 |
| 规范文档 + step99.md | 新建 |
