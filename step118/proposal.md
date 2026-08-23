# Step118 需求定义：子代理 microcompaction 工具集对齐（list_exec_sessions 入可压缩集）

## 1. 问题背景

step117 完成子代理运行时限制同步。但子代理的 `list_exec_sessions`（step114 加的会话列表工具，结果可能很长）
**不在 `COMPACTABLE_TOOLS` 内**（`governance.py:18-21`）。该集合决定 inflight 微压缩的候选工具，
主代理与子代理共用同一个 `_GOVERNOR`（`runner.py:30`）+ 同一全局 `COMPACTABLE_TOOLS`，
因此 `list_exec_sessions` 的长结果永远不会被微压缩，与 nanobot 不对等。

## 2. 本 step 要解决什么

把 `list_exec_sessions` 补入 `COMPACTABLE_TOOLS`，使主/子代理的该工具长结果都能被 inflight 微压缩，
对齐 nanobot `context_governance.py:32-35`。

## 3. 为什么这样做（方案取舍）

- 直接补齐 learn_nano 相对 nanobot 唯一缺失的一项：`list_exec_sessions`（nanobot 集合含它，learn_nano 缺）。
- 复用既有 `ContextGovernor._inflight_compaction_candidates`（`governance.py:365`）——只改 `COMPACTABLE_TOOLS`
  这一个 frozenset 即可同时覆盖主/子代理，零重写、零新依赖。
- 范围取舍：
  - **不补 `read_file`**：learn_nano 把 `read_file` 放在 `TOOL_RESULT_OFFLOAD_EXEMPT_TOOLS`（不截断、不微压缩，
    作为持久化结果的恢复路径），属既有有意设计，改动会波及其它逻辑与测试，超出本 step 范围。
  - **不补 `run_cli_app`**：nanobot 也未列入，且路线图只点名 `list_exec_sessions`，保持最小增量。

## 4. 目标与实现边界（最小增量）

- 目标：`list_exec_sessions` 进入 `COMPACTABLE_TOOLS`，其 ≥500 字工具结果可被微压缩为摘要。
- 边界（**不做**）：
  - 不改 `read_file` 的豁免/微压缩行为；
  - 不改 `run_cli_app` 或其它工具集；
  - 不改 `ContextGovernor` 的压缩算法/阈值。

## 5. 验收标准

1. `governance.py` 的 `COMPACTABLE_TOOLS` 含 `"list_exec_sessions"`。
2. 新增测试：`list_exec_sessions` 长结果被选为 inflight 微压缩候选；非可压缩工具（如 `spawn`）不入选。
3. 全量测试失败数与 step117 基线（25）持平，无新增回归。
