# Step 106 Proposal: MemoryStore Git 集成 + dream_content_diff

## 1. 问题背景

step105 已新增 GitStore 模块，但 MemoryStore 未集成。Dream 运行后的记忆文件变更无法获取差异摘要，commit message 和 cursor 推进门控缺少真实数据支撑。

## 2. 目标

1. MemoryStore `__init__` 中创建 GitStore 实例，跟踪 SOUL.md / USER.md / memory/MEMORY.md / memory/.dream_cursor
2. 新增 `git` property 返回 GitStore 实例
3. 新增 `dream_content_diff()` 方法，返回持久化记忆文件的未提交变更摘要

## 3. 非目标

- 不实现自动 commit（后续 harness 层集成）
- 不修改 Dream 运行流程
- 不修改 GitStore 实现（step105 已完成）

## 4. 验收标准

1. MemoryStore 初始化后 `_git` 为 GitStore 实例
2. `git` property 返回该实例
3. `dream_content_diff()` git 未初始化时返回空串
4. `dream_content_diff()` 无变更时返回空串
5. `dream_content_diff()` 有变更时返回结构化摘要（包含文件名和变更内容）
6. 变更提交后 `dream_content_diff()` 返回空串
7. tracked_files 包含 SOUL.md / USER.md / memory/MEMORY.md / memory/.dream_cursor
8. 单元测试通过
