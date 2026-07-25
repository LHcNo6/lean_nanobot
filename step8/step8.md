# Step 8 — 自动压缩（Token-aware Consolidation）

## 目标

为 step7 的 Session 加上 **token 预算控制**：超出预算时自动压缩旧消息、用 LLM 总结摘要、注入 system prompt。

## 文件结构

```
step8/
├── __init__.py
├── llm.py, provider.py, openai_compat_provider.py    # from step7
├── tool.py, tools/echo.py                             # from step7
├── runner.py                                           # from step7
├── session.py                  ★ 修改: get_history(max_tokens=...)
├── context.py                  ★ 修改: build_system_prompt(session_summary=...)
├── consolidation.py            ★ NEW: TokenEstimator + Consolidator
├── main.py                     ★ NEW: token-budget-aware CLI
├── test.py                     ★ NEW: 21 个测试
└── step8.md
```

## TokenEstimator

```python
def estimate_message_tokens(msg: dict) -> int:
    # content (str / list[text blocks]) + name + tool_call_id + tool_calls
    # ≈ len(payload) // 4 + 4
    return max(4, len(payload) // 4 + 4)

def estimate_prompt_tokens(messages: list[dict]) -> int:
    return sum(estimate_message_tokens(m) for m in messages) + 4 * len(messages)
```

字符级估算，不依赖 tiktoken。

## Consolidator

```python
@dataclass
class Consolidator:
    provider: LLMProvider | None = None    # None = 无摘要（只截断）
    consolidation_ratio: float = 0.5       # 压缩后保留 50% 的预算

    async def maybe_consolidate(session, max_tokens, model=None) -> str | None
```

**流程**：
```
maybe_consolidate(session, max_tokens):
  │
  ├─ 1. unconsolidated = session.messages[last_consolidated:]
  ├─ 2. estimated = estimate_prompt_tokens(unconsolidated)
  ├─ 3. target = max_tokens × consolidation_ratio
  ├─ 4. 如果 estimated ≤ target → return None（无需压缩）
  │
  ├─ 5. _find_boundary(unconsolidated, target)
  │     从后往前累加 token，找到能放入 target 的位置
  │     再对齐到最近的 user 轮次
  │
  ├─ 6. 如果有 provider:
  │     _archive(to_archive) → LLM 摘要 → summary text
  │
  ├─ 7. session.last_consolidated += boundary
  ├─ 8. 如果产生摘要: session.metadata["_last_summary"] = {...}
  └─ 9. return summary
```

`_archive()` 使用硬编码的 system prompt 让 LLM 总结对话片段的 key facts。

## `get_history(max_tokens)` 改造

```python
# Session.get_history() 新增 max_tokens 参数
def get_history(self, max_messages=50, max_tokens=0):
    unconsolidated = self.messages[self.last_consolidated:]
    
    if max_tokens > 0:
        # 从后往前，在 token 预算内保留尾部
        for msg in reversed(unconsolidated):
            ...
        unconsolidated = kept
    
    if max_messages > 0:
        unconsolidated = unconsolidated[-max_messages:]
    
    return list(unconsolidated)
```

注意顺序：先按 token 切，再按条数切。

## `build_system_prompt(session_summary)` 改造

```python
def build_system_prompt(self, identity=None, session_summary=None):
    parts = [identity or _DEFAULT_IDENTITY]
    # bootstrap files...
    if session_summary:
        parts.append(f"[Archived Context Summary]\n\n{session_summary}")
    return "\n\n---\n\n".join(parts)
```

摘要出现在 system prompt 末尾。

## 集成流程

```python
# 1. 尝试压缩
summary = await consolidator.maybe_consolidate(session, max_tokens=budget)

# 2. 获取 token 预算内的 history
history = session.get_history(max_messages=50, max_tokens=budget)

# 3. 构建 context（含摘要）
msgs = context.build_messages(message, history=history, session_summary=summary)

# 4. 运行 AgentRunner
result = await AgentRunner().run(spec)

# 5. 保存新消息
session.import_messages(result.messages[1 + len(history):])
session_manager.save(session)
```

## 与 nanobot 对比

| 特性 | nanobot | step8 |
|---|---|---|
| Token 估算 | tiktoken cl100k_base | 字符级 (len//4) |
| consolidation_ratio | 0.5（可配置） | 0.5（硬编码） |
| 边界对齐 | `pick_consolidation_boundary()` + user 轮次 | `_find_boundary()` + user 对齐 |
| 摘要 prompt | `consolidator_archive.md` Jinja2 模板 | 硬编码字符串 |
| 多轮压缩 | 循环 5 轮直到 ≤ target | 单轮 |
| 摘要存储 | `metadata["_last_summary"]` | 同 |
| 摘要注入 | `build_system_prompt(session_summary=...)` | 同 |
| 压缩触发时机 | BUILD 阶段 + 后台 | 每次 turn 前主动调用 |
| history.jsonl | `MemoryStore.append_history()` | 暂未实现（step15） |
| Dream 蒸馏 | 有 | 无 |

## 测试覆盖（21 个）

| # | 测试 | 场景 |
|---|------|------|
| 1–5 | Token 估算 | 文本/tool_calls/tool_result/prompt_tokens |
| 6–9 | get_history(max_tokens) | 限制/全部/zero/last_consolidated |
| 10–11 | _find_boundary | 全部保留/截断部分 |
| 12–15 | maybe_consolidate | 不超预算/无 provider/有 provider/无消息 |
| 16–17 | _format_messages | 普通/含 tool_calls |
| 18–20 | session_summary | 在 system prompt/in build_messages/无摘要 |
| 21 | 完整集成 | Consolidator → get_history → build_messages |

## 暴露的问题

1. **单轮压缩** — 如果剩余历史仍然超预算，需要外部循环调用
2. **字符级 token 估算不准** — 中文和英文混用时偏差较大（后续可加 tiktoken）
3. **摘要 prompt 硬编码** — 不是模板文件，用户无法自定义
4. **与 step7 main.py 的兼容性** — `get_history` 签名扩展了 `max_tokens`，但默认为 0（向后兼容）
5. **main.py 的 `/new` 访问私有属性** — 同 step7

## 下一步

**Step 9：MessageBus** — `asyncio.Queue` 驱动 inbound/outbound 消息通道，为多通道做准备。
