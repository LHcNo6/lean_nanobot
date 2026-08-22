# Step 96 Design: 数据校验层 + cursor 对齐

## 原理

### _valid_cursor
Python 中 `isinstance(True, int)` 为 True，bool 会被误判为 int。必须显式拒绝 bool。只接受非负 int。

### _valid_history_payload
每个 entry 必须有 str 类型的 timestamp 和 content，session_key 可选但必须是 str。

### _iter_valid_entries
生成器模式，遍历 `_read_entries()`，对每个 entry 做 cursor 和 payload 双重校验。无效 entry 跳过，首次遇到时记录 warning（用 flag 限流）。返回 `(entry, cursor)` 元组，方便下游同时使用 entry 和已校验的 cursor。

### _next_cursor 对齐
参考实现的逻辑：
1. 读 cursor_counter（.cursor 文件）
2. 读 last entry 的 cursor
3. 如果 cursor_counter 存在：取 max(counter, last_cursor) + 1；如果 last 无效则扫描全文件取 max
4. 如果 cursor_counter 不存在：last 有效则 last+1；否则扫描全文件取 max

### get_latest_cursor 对齐
参考实现：`max(self._next_cursor() - 1, 0)`。因为 `_next_cursor` 返回下一个可用 cursor，减 1 就是最新已分配的 cursor。

## 文件修改清单

| 文件 | 操作 |
|------|------|
| `memory.py` | 修改：+3 个校验方法 +2 个 flag +read_unprocessed_history/_next_cursor/get_latest_cursor 改写 |
| `tests/test_memory_validation.py` | 新建 |
| 规范文档 + step96.md | 新建 |
