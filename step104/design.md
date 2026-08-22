# Step 104 Design: dream_run_completed + build_dream_commit_message

## 实现思路

### 1. dream_run_completed(resp) 静态方法

```python
@staticmethod
def dream_run_completed(resp: object | None) -> bool:
    metadata = getattr(resp, "metadata", None)
    return isinstance(metadata, dict) and metadata.get("_stop_reason") == "completed"
```

防御式检查：resp 为 None、无 metadata 属性、metadata 非 dict、无 _stop_reason 键、值不等于 "completed" 均返回 False。

### 2. build_dream_commit_message(prefix, diff_body) 静态方法

```python
@staticmethod
def build_dream_commit_message(prefix: str, diff_body: str) -> str:
    diff_body = (diff_body or "").strip()
    if not diff_body:
        return prefix
    return f"{prefix}\n\n{diff_body}"
```

空 diff（None/空串/纯空白）返回纯 prefix，避免无意义的空行。diff_body 来自 `dream_content_diff()` 的真实 git 工作树摘要，而非 LLM 自报告。

### 3. main.run_dream 集成

在 `run_result = await loop._runner.run(spec)` 后，用 `MemoryStore.dream_run_completed(run_result)` 判断：
- 完成 → 推进 `set_last_dream_cursor(last_cursor)`
- 未完成 → 不推进 cursor，下次 Dream 继续处理同一批历史

## 文件修改清单

| 文件 | 操作 |
|------|------|
| `memory.py` | 修改：+2 个静态方法 |
| `main.py` | 修改：run_dream 用 dream_run_completed 判断 cursor 推进 |
| `tests/test_dream_run_helpers.py` | 新建（11 测试） |
| `proposal.md` / `design.md` / `api-spec.md` | 新建 |
