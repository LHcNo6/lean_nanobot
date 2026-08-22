# Step 93 Design: get_memory_context + context.py 集成

## 1. 原理分析

### 1.1 为什么需要 get_memory_context

MEMORY.md 存储跨会话的长期事实、偏好和决策。模型每次对话都需要感知这些
信息，否则长期记忆形同虚设。参考实现将 MEMORY.md 内容包装为
`## Long-term Memory\n{content}` 格式注入 system prompt，与 bootstrap
文件（AGENTS.md / SOUL.md / USER.md）并列。

### 1.2 为什么用惰性 memory 属性

step93 的 ContextBuilder 是 dataclass，workspace 是 str 类型。如果在
`__init__` 中直接创建 MemoryStore，会触发 `ensure_dir` 创建 memory/ 目录，
这在纯测试场景（不涉及记忆）下是不必要的副作用。因此采用惰性 property：
首次访问 `self.memory` 时才创建 MemoryStore 实例并缓存。

### 1.3 注入位置

参考实现在 bootstrap 文件之后、skills 之前注入 `# Memory` 段。step93
保持相同顺序，在 bootstrap_files 循环之后、skills 注入之前插入长期记忆段。

### 1.4 与参考实现的差异

参考实现还有 `_is_template_content` 检查（判断 MEMORY.md 是否是未修改的
模板内容）和 `# Recent History` 段（近期历史注入）。这两项分别属于更
高级的功能，本 step 不实现，留待后续 step。

## 2. 文件修改清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `memory.py` | 修改 | 新增 get_memory_context() 方法 |
| `context.py` | 修改 | 新增惰性 memory 属性 + build_system_prompt 注入长期记忆 |
| `tests/test_memory_context.py` | 新建 | 单元测试 |
| `proposal.md` / `design.md` / `api-spec.md` | 新建 | 规范文档 |
| `step93.md` | 新建 | 配套文档 |
