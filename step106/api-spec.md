# Step 106 API 契约

## memory.py — MemoryStore 变更

### 新增类常量
```python
_DREAM_CONTENT_PATHS: tuple[str, ...] = ("SOUL.md", "USER.md", "memory/MEMORY.md")
```

### __init__ 变更
新增实例字段 `_git: GitStore`，在初始化末尾创建：
```python
self._git = GitStore(workspace, tracked_files=[
    "SOUL.md", "USER.md", "memory/MEMORY.md", "memory/.dream_cursor",
])
```

### 新增属性
```python
@property
def git(self) -> GitStore
```
返回 `self._git` 实例。

### 新增方法
```python
def dream_content_diff(self) -> str
```
返回持久化记忆文件（SOUL.md / USER.md / memory/MEMORY.md）的未提交变更结构化摘要。git 未初始化或无变更时返回空串。
