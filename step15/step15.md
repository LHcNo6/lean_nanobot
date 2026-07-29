# Step 15 — Consolidation + Dream + MemoryStore

## 目标

在 Step 14（Context Governance）的基础上：

1. **重构 Consolidation** — 从简单的 `maybe_consolidate(session, max_tokens)` 改为 token-budget-driven 的 `maybe_consolidate_by_tokens(session, *, runtime)`，支持多轮压缩和持久化归档
2. **MemoryStore** — 新增 `memory/` 目录管理历史摘要归档、Dream cursor 跟踪，提供 `append_history`、`raw_archive`、`build_dream_prompt` 等能力
3. **Dream** — 自动化记忆整理：周期性地回顾未处理的归档摘要，更新 SOUL.md / USER.md / memory/MEMORY.md
4. **Runtime dataclass** — 将 `context_window_tokens`、`max_tokens`、`provider`、`model` 打包，作为 consolidation 和 archive 的统一配置传递

## 改动文件

| 文件 | 变化 |
|------|------|
| `memory.py` | **新增** — `MemoryStore` 类管理 history.jsonl、cursor、dream cursor、raw archive |
| `llm.py` | **新增** `Runtime` dataclass（context_window_tokens, max_tokens, provider, model） |
| `consolidation.py` | **重写** — `Consolidator` 接受 store/sessions/build_messages/get_tool_definitions；新增 `maybe_consolidate_by_tokens`、`archive`、`compact_idle_session`、`pick_consolidation_boundary` |
| `loop.py` | **修改** — 接受 `memory` 而非 `consolidator`；构造函数内部创建 Consolidator；新增 `run_dream()` 方法；`_state_compact` 调用 `maybe_consolidate_by_tokens`；`_state_save` 调度后台 consolidation |
| `main.py` | **修改** — 使用 `MemoryStore`；启动 `_dream_loop` 后台任务；新增 `/dream` 命令 |
| `test.py` | 新增 24 个测试（MemoryStore、Dream、Consolidator new API、Runtime），共 128 个 |

## 设计

### MemoryStore

```
MEMORY.md
history.jsonl        ← archive 记录（每个 entry 含 cursor, timestamp, content, session_key）
.cursor              ← 最新使用的 cursor 值
.dream_cursor        ← Dream 上次处理的 cursor 位置
```

- `append_history(entry, max_chars, session_key)` — 追加一条归档记录，自动生成 cursor
- `read_unprocessed_history(since_cursor)` — 读取 cursor 之后的条目
- `raw_archive(messages)` — 将原始消息格式化为 `[RAW]...` 文本后归档
- `compact_history()` — 超出 `max_history_entries` 时裁减最旧条目
- `build_dream_prompt(max_entries=20)` — 构建 Dream 提示词（包含当前 SOUL/USER/MEMORY.md + 未处理历史）

### Consolidator (New API)

```
maybe_consolidate_by_tokens(session, *, runtime)
  │
  ├─ 加 per-session lock
  ├─ 从 SessionManager 获取最新 session
  ├─ 计算 input_token_budget = context_window_tokens - max_tokens - 1024
  ├─ target = budget * consolidation_ratio
  ├─ 估算 unconsolidated token → 若 <= budget 或 <= 0 则跳过
  │
  └─ 最多 _MAX_CONSOLIDATION_ROUNDS 轮：
       ├─ pick_consolidation_boundary → 找到切割点
       ├─ archive(chunk, runtime=runtime) → 调用 runtime.provider.chat 生成摘要
       │    └─ 成功：append_history(summary)
       │    └─ 失败：raw_archive(chunk) 保存原始消息
       ├─ 更新 session.last_consolidated
       ├─ 保存 session
       └─ 若 estimated ≤ target 则退出
```

### Runtime

```python
@dataclass
class Runtime:
    context_window_tokens: int
    max_tokens: int = 4096
    provider: Any = None
    model: str | None = None
```

### Dream 流程

```
Dream 循环（main.py 后台，每 _DREAM_INTERVAL_SECONDS 触发）：
  1. build_dream_prompt() → 读取未处理归档 + 当前 memory 文件
  2. 若无新条目 → 跳过
  3. AgentRunner.run(dream_prompt) → LLM 输出记忆更新
  4. set_last_dream_cursor() → 标记已处理
```

### 多轮压缩

`maybe_consolidate_by_tokens` 执行最多 5 轮压缩，每轮：

1. 估算当前 unconsolidated token 超出 budget 多少
2. `pick_consolidation_boundary` 找到能移除 `estimated - target` tokens 的切割点
3. 调用 `archive` 归档切割段
4. 更新 `last_consolidated` 和 `_last_summary`

### 闲置会话压缩

`compact_idle_session(session_key, *, runtime, max_suffix=8)`：

1. 获取最新 session
2. 保留最后 `max_suffix` 条未处理消息
3. 归档前面的消息
4. 重置 `last_consolidated = 0`
5. 清除非保留消息

## 与 step14 的关键差异

| 方面 | step14 | step15 |
|------|--------|--------|
| Consolidation API | `maybe_consolidate(session, max_tokens, model)` | `maybe_consolidate_by_tokens(session, *, runtime)` |
| 归档存储 | 无持久化 | `MemoryStore`（history.jsonl + raw_archive） |
| Consolidator 构建 | `Consolidator(provider=...)` | `Consolidator(store, sessions, build_messages, get_tool_definitions)` |
| AgentLoop 参数 | `consolidator` | `memory`（内部创建 Consolidator） |
| Provider 传递 | 构造函数直接传 | 通过 `Runtime.provider` 传入 |
| 多轮压缩 | 不支持 | 最多 5 轮 |
| Dream | 不支持 | `run_dream()` + 后台 `_dream_loop` |
| Session 锁 | loop 级 | Consolidator 内部 per-session |
| 测试数 | 104 | 128 |

## 下一站

Step 16 — Subagents + Sustained Goals
