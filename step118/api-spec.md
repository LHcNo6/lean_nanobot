# Step118 接口契约（api-spec）

本文件定义 step118「子代理 microcompaction 工具集对齐（list_exec_sessions 入可压缩集）」的契约。

## D1：COMPACTABLE_TOOLS 含 list_exec_sessions

```python
# step118/governance.py
COMPACTABLE_TOOLS = frozenset({
    "exec", "grep", "find_files",
    "web_search", "web_fetch", "list_dir",
    "list_exec_sessions",
})
```

契约：`"list_exec_sessions" in COMPACTABLE_TOOLS` 为真；其余既有 6 项不变。

## D2：list_exec_sessions 长结果入选 inflight 微压缩候选

`ContextGovernor._inflight_compaction_candidates(config, messages, compacted_tool_call_ids)`：

- 对 `role=="tool"` 且 `name=="list_exec_sessions"`、内容 `str` 且 `len≥MICROCOMPACT_MIN_CHARS(500)`、
  且 `tool_call_id` 未在 `compacted_tool_call_ids` 中的消息，返回候选 `(idx, tool_call_id)`。
- 对 `name` 不在 `COMPACTABLE_TOOLS` 的工具（如 `spawn`、`read_file` 在 learn_nano 下）不入选。

## D3：下游行为不变

`compact_inflight_overflow` / `_summary_for` 等算法与阈值（`MICROCOMPACT_KEEP_RECENT`、`INFLIGHT_COMPACT_TARGET_RATIO`）
均不变；仅候选集合扩大一项。

## D4：测试映射

| 契约 | 测试 |
| --- | --- |
| D1 | `"list_exec_sessions" in COMPACTABLE_TOOLS` |
| D2 | `list_exec_sessions` 长结果入选候选；`spawn` 长结果不入选 |
| D3 | 单测不修改算法，仅验证候选选择（算法回归由现有行为保证） |

> 全部测试使用构造数据，禁止真实网络与 API 调用。
