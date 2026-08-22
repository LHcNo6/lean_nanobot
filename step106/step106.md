# step106：MemoryStore Git 集成 + dream_content_diff

## 解决的问题

MemoryStore 未集成 GitStore，缺少 dream_content_diff 方法用于获取记忆文件变更摘要。

## 实现

1. `__init__` 中创建 GitStore 实例（tracked: memory/MEMORY.md, SOUL.md, USER.md）
2. 新增 `git` property 返回 GitStore
3. 新增 `dream_content_diff()` 方法：git 未初始化或无变更时返回空串，否则返回 summarize_working_tree

## 文件修改清单

| 文件 | 操作 |
|------|------|
| `memory.py` | 修改：+GitStore 导入 +_git 实例 +git property +dream_content_diff |
| `tests/test_memory_git.py` | 新建（7 测试） |
| 规范文档 + step106.md | 新建 |

## 测试结果

7 passed in 1.80s

## 下一步

**step107**：`build_dream_tools()` 方法。
