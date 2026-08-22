# Step 95 Design: append_history 安全增强

## 原理

### strip_think 集成
`strip_think` 移除内联思考块与未闭合/畸形标签。在持久化前调用，确保 history.jsonl 中不包含模板泄漏内容。关键约束：raw 非空但 strip 后为空时，必须持久化空串而非回退到 raw——否则 strip_think 的保证会被历史回放撤销。

### 日志限流
超限条目通常意味着调用方忘记设置自己的 cap。每次都警告会刷屏，因此用 `_oversize_logged` flag 做首次警告后限流。

## 实现

```python
from step95.helpers import strip_think, truncate_text
import logging
logger = logging.getLogger(__name__)

# __init__ 中:
self._oversize_logged = False

# append_history 中:
if len(raw) > limit:
    if not self._oversize_logged:
        self._oversize_logged = True
        logger.warning("history entry exceeds %d chars (%d); truncating", limit, len(raw))
    raw = truncate_text(raw, limit)
content = strip_think(raw)
with self._append_lock:
    cursor = self._next_cursor()
    if raw and not content:
        logger.debug("history entry %d stripped to empty", cursor)
    record = {"cursor": cursor, "timestamp": ts, "content": content}
    ...
```

## 文件修改清单

| 文件 | 操作 |
|------|------|
| `memory.py` | 修改：+strip_think 导入 +_oversize_logged +append_history 安全增强 |
| `tests/test_memory_append_safety.py` | 新建 |
| 规范文档 + step95.md | 新建 |
