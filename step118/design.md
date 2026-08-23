# Step118 架构设计：子代理 microcompaction 工具集对齐（list_exec_sessions 入可压缩集）

## 1. 总体思路

`governance.py` 的 `COMPACTABLE_TOOLS` 是 inflight 微压缩的唯一候选来源，`_inflight_compaction_candidates`
（`governance.py:355-380`）只挑选 `name in COMPACTABLE_TOOLS` 且内容 ≥ `MICROCOMPACT_MIN_CHARS`(500) 的
tool 结果，将其 content 替换为 `_summary_for` 摘要（`[Prior {name} result compacted to fit context; ...]`）。

主/子代理共用 `_GOVERNOR`（`runner.py:30`）与全局 `COMPACTABLE_TOOLS`，故**只补集合一项即可同时覆盖子代理**。

## 2. 改动点（governance.py）

```python
COMPACTABLE_TOOLS = frozenset({
    "exec", "grep", "find_files",
    "web_search", "web_fetch", "list_dir",
    "list_exec_sessions",   # step118：对齐 nanobot，子代理会话列表长结果可微压缩
})
```

- 仅追加 1 项；`read_file` 维持现状（在 `TOOL_RESULT_OFFLOAD_EXEMPT_TOOLS`，不进微压缩集）。
- `_inflight_compaction_candidates`、`compact_inflight_overflow` 等下游逻辑无需改动。

## 3. 数据流

```
list_exec_sessions 返回长结果（role=tool, name="list_exec_sessions", content≥500字）
  └─ _GOVERNOR.prepare_for_model → compact_inflight_overflow
       └─ _inflight_compaction_candidates：name in COMPACTABLE_TOOLS 命中
            └─ content 替换为 _summary_for 摘要
```

子代理经 `self.runner.run` 走同一 `_GOVERNOR`，自动受益。

## 4. 利弊与风险

- 利：子代理 `list_exec_sessions` 长结果不再撑爆上下文，与 nanobot 对齐；改动极小、零回归面。
- 风险/注意：
  - `read_file` 仍不微压缩（learn_nano 有意豁免），若未来要与其 nanobot 完全一致需单独评估。
  - 仅影响 ≥500 字结果；短结果不受影响。

## 5. 不在本 step 范围

- `read_file` 加入 `COMPACTABLE_TOOLS`（nanobot 含，但 learn_nano 设计差异，留独立评估）。
- `run_cli_app` 加入可压缩集（nanobot 未列，非本 step 范围）。

## 6. 下一 step（step119，可选）

self/my 工具可观测子代理状态：让 `self`/`my` 工具读取 `SubagentManager._task_statuses`，
父代理可查询运行中子代理，对齐 nanobot `self.py`。
