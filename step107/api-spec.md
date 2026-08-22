# Step 107 API 契约

## memory.py — MemoryStore 新增

### build_dream_tools
```python
def build_dream_tools(self) -> list[dict]
```

返回 Dream 运行专用的受限工具定义列表（OpenAI function calling 格式）。

**包含工具：**

| 工具名 | 权限 | 说明 |
|--------|------|------|
| `read_file` | 读：整个 workspace | 读取文件内容 |
| `write_file` | 写：skills/ 目录 | 创建/覆盖文件 |
| `edit_file` | 写：skills/ 目录 + SOUL.md + USER.md + memory/MEMORY.md | 替换文件内容 |

**不包含：** shell / exec / 网络请求 / 搜索等危险工具。

**每个工具定义格式：**
```python
{
    "type": "function",
    "function": {
        "name": str,
        "description": str,
        "parameters": { ... JSON Schema ... },
    }
}
```

**副作用：** 调用时确保 `workspace/skills/` 目录存在（不存在则创建）。
