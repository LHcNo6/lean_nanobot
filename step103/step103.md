# step103：dream_session_key + prune_dream_sessions

## 解决的问题

缺少 Dream 运行的 session key 生成和旧 Dream session 清理机制。

## 实现

1. `dream_session_key()` 静态方法：返回 `dream:{YYYYMMDD-HHMMSS}` 格式的唯一 key
2. `prune_dream_sessions(sessions_dir, *, keep=10)` 静态方法：清理最旧的 Dream session 文件，只保留最近 N 个
3. 使用 `SessionManager._decode_storage_key` 识别 dream session 文件

## 文件修改清单

| 文件 | 操作 |
|------|------|
| `memory.py` | 修改：+2 个静态方法 |
| `tests/test_dream_session.py` | 新建（6 测试） |
| 规范文档 + step103.md | 新建 |

## 测试结果

6 passed in 0.17s

## 下一步

**step104**：`dream_run_completed()` + `build_dream_commit_message()` + main.py 集成。
