# Step 103 Design: dream_session_key + prune_dream_sessions

## 实现思路

### 1. dream_session_key() 静态方法

```python
@staticmethod
def dream_session_key() -> str:
    return f"dream:{datetime.now():%Y%m%d-%H%M%S}"
```

统一格式，替代 main.py 中的内联生成。

### 2. prune_dream_sessions() 静态方法

```python
@staticmethod
def prune_dream_sessions(sessions_dir: Path, *, keep: int = 10) -> None:
```

实现逻辑：
1. 遍历 `sessions_dir/*.jsonl`
2. 用 `SessionManager._decode_storage_key(path.stem)` 解码 session key
3. 筛选以 `dream:` 开头的文件
4. 按 `st_mtime` 升序排序
5. 超过 keep 数量的最旧文件删除
6. 删除失败时 `logger.warning`，不中断

### 3. main.run_dream 集成

将内联的 `dream_key = f"dream:{datetime.now().strftime('%Y%m%d-%H%M%S')}"` 替换为 `dream_key = MemoryStore.dream_session_key()`。

## 文件修改清单

| 文件 | 操作 |
|------|------|
| `memory.py` | 修改：+2 个静态方法 |
| `main.py` | 修改：run_dream 改用 dream_session_key() |
| `tests/test_dream_session.py` | 新建（6 测试） |
| `proposal.md` / `design.md` / `api-spec.md` | 新建 |
