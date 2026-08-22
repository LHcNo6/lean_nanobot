# Step 103 Proposal: dream_session_key + prune_dream_sessions

## 1. 问题背景

Dream 运行的 session key 当前在 `main.run_dream` 中内联生成（`f"dream:{datetime.now().strftime('%Y%m%d-%H%M%S')}"`），且旧 Dream session 文件不会自动清理，长期运行会导致 sessions 目录堆积。

## 2. 目标

1. 新增 `dream_session_key()` 静态方法，统一 Dream session key 生成格式
2. 新增 `prune_dream_sessions(sessions_dir, *, keep=10)` 静态方法，自动清理最旧的 Dream session 文件
3. `main.run_dream` 改用 `MemoryStore.dream_session_key()`

## 3. 非目标

- 不修改 Dream 运行逻辑
- 不修改非 dream 会话的清理策略

## 4. 验收标准

1. `dream_session_key()` 返回 `dream:{YYYYMMDD-HHMMSS}` 格式字符串
2. `prune_dream_sessions` 只清理 dream: 前缀的 session 文件
3. 保留最近 N 个（默认 10）dream session，删除更旧的
4. 非 dream session 文件不受影响
5. 无 dream 文件时不报错
6. main.run_dream 使用新方法生成 key
7. 单元测试通过
