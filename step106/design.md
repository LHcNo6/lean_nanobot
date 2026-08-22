# Step 106 Design: MemoryStore Git 集成 + dream_content_diff

## 实现思路

### 1. __init__ 集成 GitStore

在 `MemoryStore.__init__` 末尾添加：

```python
from step106.utils.gitstore import GitStore

self._git = GitStore(workspace, tracked_files=[
    "SOUL.md",
    "USER.md",
    "memory/MEMORY.md",
    "memory/.dream_cursor",
])
```

tracked_files 包含 3 个持久化内容文件 + dream cursor 文件。cursor 文件被跟踪是为了让 dream_content_diff 能感知 cursor 推进，但参考实现中 `_DREAM_CONTENT_PATHS` 只包含前 3 个内容文件（cursor 推进不算"productive edit"）。

### 2. git property

```python
@property
def git(self) -> GitStore:
    return self._git
```

### 3. dream_content_diff 方法

```python
def dream_content_diff(self) -> str:
    if not self._git.is_initialized():
        return ""
    return self._git.summarize_working_tree(list(self._DREAM_CONTENT_PATHS))
```

`_DREAM_CONTENT_PATHS = ("SOUL.md", "USER.md", "memory/MEMORY.md")`，只包含内容文件，不包含 .dream_cursor。这样 cursor 推进本身不会被误认为是有意义的编辑。

### 4. 设计取舍

- **懒加载 vs 初始化时创建**：参考实现在 `__init__` 中直接创建 GitStore。GitStore 的 `__init__` 不执行 git 命令（只存路径），所以无性能开销。
- **_DREAM_CONTENT_PATHS 常量**：定义为类常量，与参考实现对齐，用于 dream_content_diff 和后续 cursor 推进门控。

## 文件修改清单

| 文件 | 操作 |
|------|------|
| `memory.py` | 修改：+GitStore 导入 +_git 实例 +git property +dream_content_diff +_DREAM_CONTENT_PATHS |
| `tests/test_memory_git.py` | 新建（7 测试） |
| `proposal.md` / `design.md` / `api-spec.md` | 新建 |
