# Step 108 Proposal: Legacy HISTORY.md 迁移

## 1. 问题背景

旧版 nanobot 使用 `memory/HISTORY.md` 存储对话历史（带时间戳的 Markdown 格式），新版改用 `memory/history.jsonl`（JSONL 格式，带 cursor）。升级用户的旧数据无法自动迁移，会导致历史丢失或 Dream 重复处理。

## 2. 目标

实现 Legacy HISTORY.md → history.jsonl 的自动迁移机制：
1. MemoryStore 初始化时检测是否存在 HISTORY.md 且 history.jsonl 为空
2. 解析 HISTORY.md 的条目（带时间戳的 Markdown 块）
3. 写入 history.jsonl，分配 cursor
4. 设置 cursor 和 dream_cursor 为最后一条（避免重放旧数据）
5. 将原 HISTORY.md 重命名为 .bak 备份

## 3. 非目标

- 不修改 history.jsonl 的读写逻辑
- 不实现反向迁移（jsonl → md）
- 不处理 HISTORY.md 的增量更新（迁移是一次性的）

## 4. 验收标准

1. 无 HISTORY.md 时不执行迁移
2. history.jsonl 已存在且非空时不执行迁移
3. 单条目 HISTORY.md 正确迁移为 1 条 jsonl 记录
4. 多条目正确迁移，cursor 从 1 递增
5. 迁移后 cursor 文件和 dream_cursor 文件设为最后一条 cursor
6. 原 HISTORY.md 被重命名为 HISTORY.md.bak（已存在则 .bak.2 等）
7. 无时间戳的条目使用文件 mtime 作为 fallback
8. [RAW] 块内的多行消息不被错误拆分
9. 单元测试通过
