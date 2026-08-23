# Step118：子代理 microcompaction 工具集对齐（list_exec_sessions 入可压缩集）

## 1. 问题背景

step117 完成子代理运行时限制同步。但子代理的 `list_exec_sessions`（step114 加的会话列表工具，结果可能很长）
**不在 `COMPACTABLE_TOOLS` 内**（`governance.py:18-21`）。该集合是 inflight 微压缩的唯一候选来源，
`_inflight_compaction_candidates`（`governance.py:365`）只挑选 `name in COMPACTABLE_TOOLS` 且内容 ≥500 字
的 tool 结果替换为摘要。主/子代理共用同一个 `_GOVERNOR`（`runner.py:30`）+ 同一全局 `COMPACTABLE_TOOLS`，
因此 `list_exec_sessions` 的长结果永远不会被微压缩，与 nanobot 不对等。

## 2. 这一 step 解决了什么 / 为什么这样做

把 `list_exec_sessions` 补入 `COMPACTABLE_TOOLS`，使主/子代理的该工具长结果都能被 inflight 微压缩，
对齐 nanobot `context_governance.py:32-35`。

方案取舍：
- 直接补齐 learn_nano 相对 nanobot 唯一缺失的一项；复用既有 `_inflight_compaction_candidates`，
  只改 `COMPACTABLE_TOOLS` 一个 frozenset 即同时覆盖主/子代理，零重写、零新依赖。
- **不补 `read_file`**：learn_nano 把 `read_file` 放在 `TOOL_RESULT_OFFLOAD_EXEMPT_TOOLS`（不截断、不微压缩，
  作为持久化结果的恢复路径），属既有有意设计，改动会波及其它逻辑与测试，超出本 step 范围。
- **不补 `run_cli_app`**：nanobot 也未列入，且路线图只点名 `list_exec_sessions`，保持最小增量。

## 3. 原理思路与具体实现

### 3.1 governance.py：补入一项
```python
COMPACTABLE_TOOLS = frozenset({
    "exec", "grep", "find_files",
    "web_search", "web_fetch", "list_dir",
    "list_exec_sessions",   # step118：对齐 nanobot，子代理会话列表长结果可微压缩
})
```
下游 `compact_inflight_overflow` / `_summary_for` 算法与阈值（`MICROCOMPACT_KEEP_RECENT`、
`INFLIGHT_COMPACT_TARGET_RATIO`、`MICROCOMPACT_MIN_CHARS`）均不变。

### 3.2 覆盖范围
子代理经 `self.runner.run` 走同一 `_GOVERNOR`，自动受益于该集合扩展；无需在子代理侧额外改动。

## 4. 核心函数 / 类功能说明

| 元素 | 职责 |
| --- | --- |
| `COMPACTABLE_TOOLS`（`governance.py`） | inflight 微压缩候选工具集（本 step 加入 `list_exec_sessions`） |
| `ContextGovernor._inflight_compaction_candidates` | 按 `name in COMPACTABLE_TOOLS` + 长度≥500 挑选候选 |
| `ContextGovernor._summary_for` | 把候选内容替换为摘要（算法不变） |

## 5. 暴露了什么问题 / 下一 step

- 暴露：`read_file` 在 learn_nano 下不进微压缩集（nanobot 进了），若需完全一致需独立评估（可能波及
  persisted-result 恢复路径）。
- 暴露：`run_cli_app`（step112/115 同步、输出也可能很长）未列入可压缩集，nanobot 也未列，暂不动。
- 下一 step（step119，可选）：self/my 工具可观测子代理状态——让 `self`/`my` 工具读取
  `SubagentManager._task_statuses`，父代理可查询运行中子代理，对齐 nanobot `self.py`。

## 6. 验证

- 新增 `tests/test_governance_compaction.py`：6 个用例全绿。
  - `list_exec_sessions` 在 `COMPACTABLE_TOOLS` 内；既有 6 项不丢；
  - `list_exec_sessions` 长结果（≥500 字）入选候选；短结果 / 非可压缩工具（spawn）/
    已压缩 id 不入选。
- 全量 `step118/tests`：**25 failed / 1164 passed**（与 step117 基线 25 持平，新增 6 通过，无新增回归）。
  失败用例为 Windows 既有问题，与微压缩工具集无关。
