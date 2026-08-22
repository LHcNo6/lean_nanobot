# Step 92 API 契约

## MemoryStore 新增方法

### read_file（静态方法）

```python
@staticmethod
def read_file(path: Path) -> str
```

- **功能**：读取文本文件内容，文件不存在时返回空字符串
- **参数**：`path` — 文件路径（Path 对象）
- **返回**：文件内容字符串；FileNotFoundError 时返回 `""`
- **异常**：不抛出 FileNotFoundError；其他 OSError 向上传播

### read_memory

```python
def read_memory(self) -> str
```

- **功能**：读取 MEMORY.md（长期记忆文件）
- **返回**：文件内容，不存在时返回 `""`

### write_memory

```python
def write_memory(self, content: str) -> None
```

- **功能**：覆盖写入 MEMORY.md
- **参数**：`content` — 要写入的文本内容
- **编码**：UTF-8

### read_soul

```python
def read_soul(self) -> str
```

- **功能**：读取 SOUL.md（人格/灵魂文件）
- **返回**：文件内容，不存在时返回 `""`

### write_soul

```python
def write_soul(self, content: str) -> None
```

- **功能**：覆盖写入 SOUL.md
- **参数**：`content` — 要写入的文本内容
- **编码**：UTF-8

### read_user

```python
def read_user(self) -> str
```

- **功能**：读取 USER.md（用户画像文件）
- **返回**：文件内容，不存在时返回 `""`

### write_user

```python
def write_user(self, content: str) -> None
```

- **功能**：覆盖写入 USER.md
- **参数**：`content` — 要写入的文本内容
- **编码**：UTF-8
