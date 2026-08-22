# Step 96 Proposal: 数据校验层 + cursor 对齐

## 1. 问题背景

当前 `read_unprocessed_history` 和 `_next_cursor` 直接操作 `_read_entries()` 的原始数据，不校验 entry 的合法性。如果外部程序写入了畸形数据（无效 cursor、缺少 timestamp/content 字段），会导致：
- cursor 单调性被破坏（`_next_cursor` 可能返回重复或更小的值）
- `read_unprocessed_history` 返回畸形 entry，下游消费时崩溃

参考实现引入了三层校验：
1. `_valid_cursor`：只接受非负 int，拒绝 bool（`isinstance(True, int)` 为 True 的陷阱）
2. `_valid_history_payload`：校验 timestamp/content 为 str，session_key 为 str 或 None
3. `_iter_valid_entries`：遍历生成器，跳过无效 entry，首次遇到时 `logger.warning` 后限流

同时 `get_latest_cursor` 的实现方式与参考实现不同（当前读 cursor 文件 + 读 last entry，参考实现用 `max(_next_cursor()-1, 0)`）。

## 2. 目标

1. 新增 `_valid_cursor(value)` 静态方法
2. 新增 `_valid_history_payload(entry)` 静态方法
3. 新增 `_iter_valid_entries()` 生成器方法（校验 + 警告限流）
4. 新增 `_corruption_logged` / `_malformed_entry_logged` 字段
5. `read_unprocessed_history` 改用 `_iter_valid_entries`
6. `_next_cursor` 改用 `_iter_valid_entries`（更健壮的 cursor 计算）
7. `get_latest_cursor` 改为 `max(self._next_cursor() - 1, 0)`

## 3. 非目标

- 不修改 `append_history`（step95 已完成）
- 不实现 `read_recent_history_for_prompt`（step97）
- 不修改 `_write_entries`（step94 已完成）

## 4. 验收标准

1. 无效 cursor（bool、负数、None、字符串）被跳过
2. 畸形 payload（缺 timestamp/content、类型错误）被跳过
3. 无效数据首次出现时 warning，后续限流
4. cursor 分配单调递增，即使历史文件中有无效数据
5. `get_latest_cursor` 返回正确的最新 cursor
6. 现有测试全部通过
