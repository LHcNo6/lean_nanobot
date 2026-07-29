# Step 14 — Context Governance

## 目标

在 `AgentRunner` **每次向 LLM 发送消息之前**，运行一个消息修复管线。解决三类问题：

1. **数据污染** — 历史记录中的压缩占位符、畸形 tool_call、孤儿 tool_result
2. **预算超限** — tool_result 内容过长 → 截断/持久化；总 token 超预算 → 飞行中压缩
3. **历史溢出** — 最后保底裁剪最早的非 system 消息

## 改动文件

| 文件 | 变化 |
|------|------|
| `helpers.py` | **新增** — `truncate_text`, `ensure_nonempty_tool_result`, `find_legal_message_start`, `estimate_message_tokens`, `estimate_prompt_tokens`, `estimate_prompt_tokens_chain` |
| `governance.py` | **新增** — `ContextGovernanceConfig` dataclass + `ContextGovernor` 类（~280 行） |
| `runner.py` | `AgentRunSpec.governance_config` 字段；`_run_loop` 中调用 `prepare_for_model()` |
| `test.py` | 新增 34 个测试，共 104 个 |

## 设计

### 管线

```
prepare_for_model(config, messages, compacted_tool_call_ids)
  │
  ├─ 1. strip_placeholder_assistant_messages()
  │    移除 "[Previous assistant message omitted.]" 等占位符（纯 assistant 消息）
  │
  ├─ 2. strip_malformed_tool_calls()
  │    丢弃 name=None/"" 的 tool_call（导致 provider API 400）
  │    如果清洗后既无 content 也无有效 tool_call → 整条消息丢弃
  │
  ├─ 3. drop_orphan_tool_results()
  │    tool_call_id 在历史中无对应 assistant tool_calls → 丢弃
  │
  ├─ 4. backfill_missing_tool_results()
  │    assistant 声明了 tool_call 但无 tool_result → 插入 "[unavailable]"
  │
  ├─ 5. apply_tool_result_budget(config)
  │    超 max_tool_result_chars 的 → 截断 + "... (truncated)" 后缀
  │    read_file 结果豁免（避免 persist→read→persist 循环）
  │
  ├─ 6. compact_inflight_overflow(config, compacted_tool_call_ids)
  │    估算超过 budget → 从最早的 tool_result 开始压缩
  │    压缩为 "[Prior {name} result compacted...]"
  │    保留最新 10 条 (MICROCOMPACT_KEEP_RECENT=10)
  │
  ├─ 7. snip_history(config)
  │    压缩后仍超 budget → 丢弃最早的非 system 消息
  │    system 消息永远保留
  │    寻找合法边界（以 user 消息开头 + 完整 tool 链）
  │
  ├─ 8. drop_orphan_tool_results()  (repeat)
  └─ 9. backfill_missing_tool_results() (repeat)
```

### 不可变原则

- 每次方法返回**新列表**（当有变更时），或返回**原列表引用**（无变更）
- 不会 mutate 传入的 `messages`，不改变 session 历史

### input_budget

```python
budget = context_block_limit or (context_window_tokens - max_tokens - 1024)
```

- `context_block_limit` 是硬上线
- 空则是 `context_window - max_output - 1024`
- 安全缓冲区 1024 防止估算漂移

### 飞行中压缩

1. `_apply_recorded_compactions` — 对 `compacted_tool_call_ids` 中已记录的 tool_result 应用压缩摘要
2. 估算 token → 如果 ≤ budget，返回
3. `_inflight_compaction_candidates` — 从最早开始找到 >500 字符的 tool_result
4. 逐个压缩，直到 ≤ `budget * 0.85`

### 历史裁剪

1. 提取 system 消息
2. 从尾巴开始裁减非 system 消息，直到适配剩余 budget
3. `_legal_history_tail` + `find_legal_message_start` 确保以用户消息开头

### Runner 集成

```python
# AgentRunSpec.governance_config: ContextGovernanceConfig | None

# _run_loop 顶部的 governance 模块：
compacted_tool_call_ids: set[str] = set()
for iteration in range(spec.max_iterations):
    if spec.governance_config is not None:
        messages = _GOVERNOR.prepare_for_model(
            spec.governance_config, messages, compacted_tool_call_ids,
        )
    # ... 继续 LLM 调用
```

`compacted_tool_call_ids` 跨迭代累积，避免重复压缩。

## 与 nanobot 对齐

```
nanobot/agent/context_governance.py → step14/governance.py
nanobot/utils/helpers.py             → step14/helpers.py (子集)
nanobot/agent/runner.py              → step14/runner.py
```

差异点（简化）：
- nanobot 的 `maybe_persist_tool_result` 将超大结果写入 `.tool_results/` 目录文件；step14 只做截断
- nanobot 的 `estimate_prompt_tokens_chain` 集成 tiktoken 和 provider 计数器；step14 用 char-based 估算
- nanobot 多了 `ensure_nonempty_tool_result` 在 normalize 中被调用

## 下一站

Step 15 — Consolidation + Stream + Config Integration
