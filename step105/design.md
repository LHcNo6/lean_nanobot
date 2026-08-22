# Step 105 Design: gitstore 模块

## 实现思路

### 1. CommitInfo 数据类

```python
@dataclass
class CommitInfo:
    sha: str
    message: str
    timestamp: str
```

### 2. GitStore 类

**初始化：**
```python
def __init__(self, workspace: Path, tracked_files: list[str] | None = None):
    self.workspace = workspace
    self.tracked_files = tracked_files or []
    self._git_dir = workspace / ".git"
```

**核心方法：**

- `is_initialized() -> bool`：检查 `.git` 目录是否存在
- `init() -> None`：`git init`，然后对每个 tracked 文件，若不存在则创建空文件并 `git add`
- `auto_commit(message: str) -> str | None`：`git status --porcelain` 检查 tracked 文件变更，无变更返回 None；有变更则 `git add` + `git commit -m`，返回 commit SHA
- `summarize_working_tree(paths: list[str]) -> str`：对指定路径执行 `git diff --no-color`，解析为结构化摘要（文件名 + 变更行数）；git 未初始化返回空串

**subprocess 调用：**
使用 `asyncio.create_subprocess_exec` 或同步 `subprocess.run`，设置 `cwd=workspace`，捕获 stdout/stderr。git 命令不存在时静默降级（返回空/None）。

### 3. 设计取舍

- **subprocess vs dulwich**：参考实现用 dulwich（纯 Python，无外部依赖），但 dulwich 在 Windows 上可能有兼容性问题。本 step 用 subprocess 调用系统 git，接口与 dulwich 版本兼容，后续可无缝替换。
- **tracked_files 机制**：只监控指定文件（SOUL.md / USER.md / memory/MEMORY.md / memory/.dream_cursor），避免将工作区其他文件误提交。

## 文件修改清单

| 文件 | 操作 |
|------|------|
| `utils/gitstore.py` | 新建 |
| `tests/test_gitstore.py` | 新建（12 测试） |
| `proposal.md` / `design.md` / `api-spec.md` | 新建 |
