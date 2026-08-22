# Step 99 Proposal: archive 系统提示模板化 + LLM 调用对齐

## 1. 问题背景

Consolidator.archive 当前使用硬编码的简单系统提示（`_CONSOLIDATOR_SYSTEM_PROMPT`），与参考实现的 SNIP（Signal/Novel/Important/Persistent）分类模板差异很大。LLM 调用也缺少 temperature、reasoning_effort 等参数，且不检查 finish_reason。

## 2. 目标

1. 系统提示替换为参考实现的 SNIP 分类模板内容（模块级常量，待 prompt_templates 模块引入后改为渲染）
2. LLM 调用添加 temperature 参数（runtime.generation.temperature）
3. max_tokens 改用 runtime.max_tokens（而非硬编码 1024）
4. 检查 response.finish_reason == "error" 时触发回退
5. tools=None / tool_choice=None（如 provider 支持）

## 3. 非目标

- 不引入 prompt_templates 模块（后续 step）
- 不修改 maybe_consolidate_by_tokens 的 token 估算（step100）

## 4. 验收标准

1. 系统提示包含 SNIP 分类说明
2. LLM 调用传递 temperature 参数
3. max_tokens 使用 runtime.max_tokens
4. finish_reason == "error" 时触发 raw_archive 回退
5. 现有测试全部通过
