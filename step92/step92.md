# step92：MemoryStore 持久化文件读写基础方法

## 这一阶段解决了什么问题以及为什么要这样做

step91 的 MemoryStore 只实现了 history.jsonl 的追加读写和 Dream cursor 管理，
缺少对三个持久化记忆文件（MEMORY.md、SOUL.md、USER.md）的标准化读写 API。
这导致 context.py 无法注入长期记忆（`include_memory_recent_history` 仍是 no-op），
SDK 层无法读写记忆文件，Dream 流程中编辑记忆文件后无法通过标准 API 读取验证。

参考实现 nanobot 的 MemoryStore 提供了 `read_file` 静态方法和六个读写方法，
本 step 对齐这部分基础能力，为后续 step93（get_memory_context + context 集成）
奠定基础。

## 原理思路和具体实现

### 统一的 read_file 静态方法

三个持久化文件都是可选的——新 workspace 初始化时可能不存在。如果每个 read
方法都各自 try-except FileNotFoundError，会产生重复代码。因此提取 `read_file`
静态方法，统一处理"文件不存在返回空串"的语义，其他 read 方法直接委托。

```python
@staticmethod
def read_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
```

### 六个读写方法

每个方法都是一行委托：
- `read_memory()` → `read_file(self.memory_file)`
- `write_memory(content)` → `self.memory_file.write_text(content, encoding="utf-8")`
- `read_soul()` / `write_soul()` → SOUL.md
- `read_user()` / `write_user()` → USER.md

write 方法使用 UTF-8 覆盖写入，不做原子写（原子写是 step94 针对 history.jsonl
的优化，持久化文件保持简单覆盖）。

## 该 step 的目标和实现

**目标**：在 MemoryStore 中新增 read_file 静态方法 + MEMORY.md/SOUL.md/USER.md
的 6 个读写方法。

**实现**：修改 `memory.py`，在 `__init__` 之后、`append_history` 之前插入
7 个新方法，每个方法都有完整的类型注解和中文 docstring。

## 核心函数/类功能说明

| 方法 | 功能 |
|------|------|
| `read_file(path)` | 静态方法，读取文本文件，不存在返回空串 |
| `read_memory()` | 读取 MEMORY.md（长期记忆） |
| `write_memory(content)` | 覆盖写入 MEMORY.md |
| `read_soul()` | 读取 SOUL.md（人格/灵魂） |
| `write_soul(content)` | 覆盖写入 SOUL.md |
| `read_user()` | 读取 USER.md（用户画像） |
| `write_user(content)` | 覆盖写入 USER.md |

## 文件修改清单

| 文件 | 操作 |
|------|------|
| `memory.py` | 修改：+read_file 静态方法 + 6 个读写方法 |
| `tests/test_memory_store.py` | 新建（17 测试） |
| `proposal.md` / `design.md` / `api-spec.md` | 新建 |
| `step92.md` | 新建 |

## 测试结果

17 passed in 0.23s

## 暴露了什么问题

- `include_memory_recent_history` 参数在 context.py 中仍是 no-op，需要
  step93 实现 `get_memory_context()` 并集成。
- `_write_entries` 是非原子写，存在崩溃导致文件损坏的风险（step94 解决）。
- `append_history` 未集成 `strip_think`，可能将模板泄漏写入历史（step95 解决）。

## 下一 step 要解决什么

**step93**：实现 `get_memory_context()` 方法，并在 context.py 中集成，
使长期记忆（MEMORY.md 内容）能注入到 system prompt 中，消除
`include_memory_recent_history` 的 no-op 状态。
