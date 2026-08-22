# Step 93 API 契约

## MemoryStore 新增方法

### get_memory_context

```python
def get_memory_context(self) -> str
```

- **功能**：获取长期记忆上下文，用于注入 system prompt
- **返回**：MEMORY.md 有内容时返回 `## Long-term Memory\n{content}`；
  MEMORY.md 为空或不存在时返回空字符串 `""`

## ContextBuilder 新增属性

### memory（惰性 property）

```python
@property
def memory(self) -> MemoryStore
```

- **功能**：惰性创建并返回基于 self.workspace 的 MemoryStore 实例
- **缓存**：首次访问后缓存到 `self._memory`，后续访问直接返回
- **副作用**：首次访问时创建 workspace/memory/ 目录

## build_system_prompt 行为变更

```python
def build_system_prompt(
    self,
    identity: str | None = None,
    session_summary: str | None = None,
    skill_names: list[str] | None = None,
    workspace: Path | None = None,
    include_memory_recent_history: bool = True,
) -> str
```

- **新增行为**：当 `include_memory_recent_history=True` 时，调用
  `self.memory.get_memory_context()`，若非空则追加到 parts 列表
- **注入位置**：bootstrap_files 之后、skills 注入之前
- **空内容处理**：get_memory_context 返回空串时不追加
