# Step 103 API 契约

## memory.py — MemoryStore 新增

### dream_session_key（静态方法）
```python
@staticmethod
def dream_session_key() -> str
```
返回 `dream:{YYYYMMDD-HHMMSS}` 格式的唯一会话键。

### prune_dream_sessions（静态方法）
```python
@staticmethod
def prune_dream_sessions(sessions_dir: Path, *, keep: int = 10) -> None
```
清理最旧的 Dream session 文件，只保留最近 `keep` 个。仅处理 `dream:` 前缀的 session，非 dream 文件不受影响。

## main.py — run_dream 变更

Dream session key 生成从内联改为 `MemoryStore.dream_session_key()` 调用，行为不变。
