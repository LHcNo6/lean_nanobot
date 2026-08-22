# step96：数据校验层 + cursor 对齐

## 解决的问题

当前 `read_unprocessed_history` 和 `_next_cursor` 直接操作原始数据，不校验 entry 合法性。外部写入的畸形数据会破坏 cursor 单调性或导致下游崩溃。

## 实现

1. `_valid_cursor`：只接受非负 int，显式拒绝 bool（`isinstance(True, int)` 陷阱）
2. `_valid_history_payload`：校验 timestamp/content 为 str，session_key 为 str/None
3. `_iter_valid_entries`：生成器遍历，双重校验，无效条目跳过，首次 warning 后限流
4. `read_unprocessed_history` 改用 `_iter_valid_entries`
5. `_next_cursor` 改用 `_iter_valid_entries` 做全文件扫描 fallback
6. `get_latest_cursor` 改为 `max(_next_cursor()-1, 0)`
7. 新增 `_corruption_logged` / `_malformed_entry_logged` 限流 flag

## 文件修改清单

| 文件 | 操作 |
|------|------|
| `memory.py` | 修改：+3 校验方法 +2 flag +3 方法改写 |
| `tests/test_memory_validation.py` | 新建（29 测试） |
| 规范文档 + step96.md | 新建 |

## 测试结果

48 passed（29 新 + 19 旧）in 0.71s

## 下一步

**step97**：内部会话过滤（`_is_internal_history_session`）+ `read_recent_history_for_prompt` + context.py 近期历史注入。
