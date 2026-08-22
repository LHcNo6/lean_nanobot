# step93：get_memory_context + context.py 集成

## 这一阶段解决了什么问题以及为什么要这样做

step92 新增了 MEMORY.md / SOUL.md / USER.md 的读写方法，但长期记忆
（MEMORY.md 内容）尚未注入到 LLM 的 system prompt 中。context.py 的
`include_memory_recent_history` 参数自 step41 起就是 no-op。

本 step 实现 `get_memory_context()` 方法，并在 ContextBuilder 中集成，
使长期记忆能跨会话注入到 system prompt，让模型感知持久化的事实、偏好
和决策。

## 原理思路和具体实现

### get_memory_context

读取 MEMORY.md 内容，包装为 `## Long-term Memory\n{content}` 格式。
MEMORY.md 为空或不存在时返回空字符串，调用方据此判断是否注入。

```python
def get_memory_context(self) -> str:
    long_term = self.read_memory()
    if not long_term:
        return ""
    return f"## Long-term Memory\n{long_term}"
```

### ContextBuilder 惰性 memory 属性

ContextBuilder 是 dataclass，添加 `_memory` 非 init 字段和 `memory`
property。首次访问时创建 MemoryStore 实例并缓存，避免纯测试场景下
不必要的目录创建副作用。

### build_system_prompt 注入

在 bootstrap_files 循环之后、skills 注入之前，当
`include_memory_recent_history=True` 时调用 `self.memory.get_memory_context()`，
非空则追加到 parts 列表。

## 该 step 的目标和实现

**目标**：长期记忆注入 system prompt，消除 `include_memory_recent_history` 的 no-op 状态。

**实现**：
- `memory.py`：新增 `get_memory_context()` 方法
- `context.py`：新增惰性 `memory` property + `build_system_prompt` 注入长期记忆段

## 核心函数/类功能说明

| 方法/属性 | 功能 |
|-----------|------|
| `MemoryStore.get_memory_context()` | 返回 `## Long-term Memory\n{content}` 或空串 |
| `ContextBuilder.memory` | 惰性 MemoryStore 实例 |
| `build_system_prompt(include_memory_recent_history=...)` | True 时注入长期记忆段 |

## 文件修改清单

| 文件 | 操作 |
|------|------|
| `memory.py` | 修改：+get_memory_context() |
| `context.py` | 修改：+_memory 字段 +memory property +build_system_prompt 注入 |
| `tests/test_memory_context.py` | 新建（12 测试） |
| `proposal.md` / `design.md` / `api-spec.md` | 新建 |
| `step93.md` | 新建 |

## 测试结果

29 passed（12 新 + 17 旧）in 0.53s

## 暴露了什么问题

- `_write_entries` 是非原子写，存在崩溃导致 history.jsonl 损坏的风险（step94）。
- `append_history` 未集成 `strip_think`，可能将模板泄漏写入历史（step95）。
- 近期历史（# Recent History 段）尚未注入，需要 `read_recent_history_for_prompt`（step97）。

## 下一 step 要解决什么

**step94**：将 `_write_entries` 改为原子写（tmp + fsync + os.replace + 目录 fsync），
防止进程崩溃导致 history.jsonl 文件损坏。
