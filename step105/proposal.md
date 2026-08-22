# Step 105 Proposal: gitstore 模块

## 1. 问题背景

Dream 运行后记忆文件（SOUL.md / USER.md / MEMORY.md）的变更需要自动提交到 git，且 commit message 和 cursor 推进需要基于真实工作树差异而非 LLM 自报告。当前缺少 Git 操作封装层。nanobot 使用 dulwich 纯 Python 实现，本 step 使用 subprocess 调用 git 命令以保持接口兼容。

## 2. 目标

新增 `utils/gitstore.py` 模块，包含：
1. `CommitInfo` 数据类（sha / message / timestamp）
2. `GitStore` 类：`is_initialized`、`init`、`auto_commit`、`summarize_working_tree`
3. 支持 tracked_files 配置，只监控指定文件变更

## 3. 非目标

- 不集成到 MemoryStore（step106 完成）
- 不实现分支管理、远程推送、merge 等高级 git 操作
- 不使用 dulwich（subprocess 方案，接口兼容）

## 4. 验收标准

1. `GitStore(workspace, tracked_files=[...])` 初始化正常
2. `is_initialized()` 非 git 目录返回 False
3. `init()` 执行 `git init` 并创建 tracked 文件（如不存在）
4. `init()` 幂等（已初始化不重复执行）
5. `auto_commit(message)` 无变更返回 None，有变更返回 commit SHA
6. `summarize_working_tree(paths)` 返回结构化 diff 摘要
7. 非 git 环境下 `summarize_working_tree` 返回空串
8. 单元测试通过
