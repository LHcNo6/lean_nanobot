# Step 92 Proposal: MemoryStore 持久化文件读写基础方法

## 1. 问题背景

step91 的 MemoryStore 只实现了 history.jsonl 的追加读写和 Dream cursor 管理，
缺少对三个持久化记忆文件（MEMORY.md、SOUL.md、USER.md）的标准化读写 API。
参考实现 nanobot 的 MemoryStore 提供了 `read_memory/write_memory`、
`read_soul/write_soul`、`read_user/write_user` 六个方法，以及一个通用的
`read_file` 静态方法用于统一处理文件不存在的情况。

当前 step91 中这些方法完全缺失，导致：
- context.py 无法注入长期记忆（`include_memory_recent_history` 仍是 no-op）
- SDK 层无法读写记忆文件
- Dream 流程中编辑记忆文件后无法通过标准 API 读取验证

## 2. 目标

在 `memory.py` 的 MemoryStore 类中新增：
1. `read_file(path)` 静态方法：统一读取文本文件，不存在时返回空串
2. `read_memory()` / `write_memory(content)`：MEMORY.md 读写
3. `read_soul()` / `write_soul(content)`：SOUL.md 读写
4. `read_user()` / `write_user(content)`：USER.md 读写

## 3. 非目标

- 不实现 `get_memory_context()`（step93）
- 不实现 context.py 集成（step93）
- 不实现 GitStore 集成（step105-106）
- 不修改现有 history.jsonl 相关逻辑

## 4. 验收标准

1. `read_file(path)` 文件不存在时返回空字符串，存在时返回 UTF-8 内容
2. 六个读写方法能正确读写对应文件
3. write 方法使用 UTF-8 编码覆盖写入
4. 所有方法有完整类型注解和中文 docstring
5. 单元测试通过
