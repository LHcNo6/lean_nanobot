# Step 102 Design: build_dream_prompt 模板化

## 实现思路

修改 `MemoryStore.build_dream_prompt` 方法：

**修改前：**
```python
prompt = (
    "You are a memory curator. Review conversation summaries and "
    "update the bot's memory files...\n\n"
    f"{files_section}\n\n"
    f"## Conversation History\n{history_text}"
)
```

**修改后：**
```python
template = self._dream_template()
prompt = (
    f"{template}\n\n{files_section}\n\n"
    f"## Conversation History\n{history_text}"
)
```

核心变化：硬编码字符串替换为 `self._dream_template()` 调用，其余逻辑（历史读取、batch 截断、files_section 渲染、返回值）保持不变。

## 文件修改清单

| 文件 | 操作 |
|------|------|
| `memory.py` | 修改：build_dream_prompt 改用 _dream_template() |
| `tests/test_build_dream_prompt.py` | 新建（6 测试） |
| `proposal.md` / `design.md` / `api-spec.md` | 新建 |
