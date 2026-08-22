# Step 101 API 契约

## utils/workspace_prompts.py（新增）

### 常量
```python
WORKSPACE_PROMPT_MAX_CHARS: int = 16000
```

### 函数

```python
def workspace_prompt_file(workspace: Path, name: str) -> Path
```
返回 `workspace / ".prompts" / f"{name}.md"`。

```python
def has_workspace_prompt_override(path: Path) -> bool
```
文件存在且 `st_size > 0` 时返回 True。

```python
def load_workspace_prompt_override(path: Path) -> tuple[str | None, int]
```
返回 `(text, original_chars)`。文件不存在/空返回 `(None, 0)`。超过 `WORKSPACE_PROMPT_MAX_CHARS` 时用 `truncate_text` 截断。

```python
def initialize_workspace_prompt(workspace: Path, name: str, default_content: str) -> None
```
文件不存在时创建 `.prompts` 目录并写入默认内容。

## memory.py — MemoryStore 变更

### 新增属性
```python
@property
def dream_prompt_file(self) -> Path
```
返回 workspace dream prompt 文件路径。

### 新增方法

```python
def has_dream_prompt_override(self) -> bool
```
检测是否存在自定义 dream prompt 覆盖。

```python
@staticmethod
def default_dream_prompt() -> str
```
返回默认 Dream 系统提示文本。

```python
def _dream_template(self) -> str
```
优先返回 workspace 覆盖内容（超限截断+限流警告），否则返回默认模板。

### 新增实例字段
- `_dream_prompt_oversize_logged: bool`：超限日志限流标记
