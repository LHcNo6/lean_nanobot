# Step 105 API 契约

## utils/gitstore.py（新增）

### CommitInfo（数据类）
```python
@dataclass
class CommitInfo:
    sha: str          # commit 哈希
    message: str      # commit message
    timestamp: str    # commit 时间
```

### GitStore 类

```python
class GitStore:
    def __init__(self, workspace: Path, tracked_files: list[str] | None = None)
```

#### is_initialized
```python
def is_initialized(self) -> bool
```
检查 `.git` 目录是否存在。

#### init
```python
def init(self) -> None
```
执行 `git init`，创建缺失的 tracked 文件。幂等。

#### auto_commit
```python
def auto_commit(self, message: str) -> str | None
```
- 无 tracked 文件变更 → 返回 None
- 有变更 → `git add` + `git commit -m message`，返回 commit SHA
- git 不可用 → 返回 None

#### summarize_working_tree
```python
def summarize_working_tree(self, paths: list[str]) -> str
```
返回指定路径的工作树变更结构化摘要。git 未初始化或无变更返回空串。

#### last_commit
```python
def last_commit(self) -> CommitInfo | None
```
返回最近一次提交信息，无提交返回 None。
