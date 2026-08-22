# Step 96 API 契约

## MemoryStore 新增方法

### _valid_cursor（静态）
```python
@staticmethod
def _valid_cursor(value: Any) -> int | None
```
- 非负 int 返回该值；bool/负数/其他类型返回 None

### _valid_history_payload（静态）
```python
@staticmethod
def _valid_history_payload(entry: dict[str, Any]) -> bool
```
- timestamp/content 为 str，session_key 为 None 或 str → True；否则 False

### _iter_valid_entries
```python
def _iter_valid_entries(self) -> Iterator[tuple[dict[str, Any], int]]
```
- 生成器，yield `(entry, valid_cursor)`；无效 entry 跳过并首次 warning

## 行为变更

### read_unprocessed_history
- 改用 `_iter_valid_entries`，只返回 cursor > since_cursor 的有效 entry

### _next_cursor
- 改用 `_iter_valid_entries` 做全文件扫描 fallback，cursor 计算更健壮

### get_latest_cursor
- 改为 `max(self._next_cursor() - 1, 0)`
