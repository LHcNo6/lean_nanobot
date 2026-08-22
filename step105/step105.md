# step105：gitstore 模块

## 解决的问题

缺少 Git 版本控制封装，Dream 运行后的记忆文件变更无法自动提交。

## 实现

新增 `utils/gitstore.py`，包含：
- `CommitInfo` 数据类（sha/message/timestamp）
- `GitStore` 类：`is_initialized`、`init`、`auto_commit`、`summarize_working_tree`
- 使用 subprocess 调用 git 命令（参考实现用 dulwich，接口兼容）

## 文件修改清单

| 文件 | 操作 |
|------|------|
| `utils/gitstore.py` | 新建 |
| `tests/test_gitstore.py` | 新建（12 测试） |
| 规范文档 + step105.md | 新建 |

## 测试结果

12 passed in 2.64s

## 下一步

**step106**：MemoryStore `__init__` 集成 GitStore + `git` property + `dream_content_diff()`。
