# Step 104 API 契约

## memory.py — MemoryStore 新增

### dream_run_completed（静态方法）
```python
@staticmethod
def dream_run_completed(resp: object | None) -> bool
```
返回 True 当且仅当 `resp.metadata` 是 dict 且 `resp.metadata["_stop_reason"] == "completed"`。其他所有情况（None、无 metadata、非 dict、错误的 stop_reason）均返回 False。

### build_dream_commit_message（静态方法）
```python
@staticmethod
def build_dream_commit_message(prefix: str, diff_body: str) -> str
```
- diff_body 为空（None/空串/纯空白）→ 返回纯 `prefix`
- diff_body 非空 → 返回 `f"{prefix}\n\n{diff_body.strip()}"`

## main.py — run_dream 行为变更

Dream run 完成判断从无条件推进 cursor 改为：
- `dream_run_completed(run_result)` 为 True → 推进 cursor
- 否则 → 不推进 cursor（下次重试）
