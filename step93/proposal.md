# Step 93 Proposal: get_memory_context + context.py 集成

## 1. 问题背景

step92 新增了 MEMORY.md / SOUL.md / USER.md 的读写方法，但长期记忆
（MEMORY.md 内容）尚未注入到 LLM 的 system prompt 中。context.py 的
`include_memory_recent_history` 参数自 step41 起就是 no-op（注释明确写
"等 memory 集成后填充实际逻辑"）。

参考实现 nanobot 的 ContextBuilder 在 `build_system_prompt` 中调用
`self.memory.get_memory_context()`，将 MEMORY.md 内容作为 `# Memory`
段注入 system prompt，使模型能跨会话访问长期记忆。

## 2. 目标

1. MemoryStore 新增 `get_memory_context()` 方法：返回 `## Long-term Memory\n{content}` 或空串
2. ContextBuilder 新增惰性 `memory` 属性（MemoryStore 实例）
3. `build_system_prompt` 中当 `include_memory_recent_history=True` 且 MEMORY.md 有内容时，注入长期记忆段

## 3. 非目标

- 不实现 `read_recent_history_for_prompt`（step97）
- 不实现 `_is_template_content` 模板内容检测
- 不实现近期历史注入（# Recent History 段，step97）
- 不修改现有 bootstrap_files / skills 注入逻辑

## 4. 验收标准

1. `get_memory_context()`：MEMORY.md 为空时返回空串；有内容时返回 `## Long-term Memory\n{content}`
2. ContextBuilder.memory 惰性初始化，首次访问时创建 MemoryStore
3. `build_system_prompt(include_memory_recent_history=True)` 且 MEMORY.md 有内容时，输出包含 `## Long-term Memory` 段
4. `include_memory_recent_history=False` 时不注入记忆
5. MEMORY.md 为空时不注入记忆段
6. 单元测试通过
