# Step 6 — ContextBuilder（系统提示组装）

## 目标

把 system prompt 的构建从调用者手中抽象出来，用一个 `ContextBuilder` 类统一管理：agent identity + bootstrap 引导文件 → 完整的 messages 列表。

## 文件结构

```
step6/
├── __init__.py
├── llm.py, provider.py, openai_compat_provider.py    # from step5
├── tool.py, tools/echo.py                            # from step5
├── runner.py                                          # from step5（0 改动）
├── context.py              ★ NEW: ContextBuilder
├── main.py                 ★ NEW: CLI 演示
├── test.py                 ★ NEW: 11 个测试
├── AGENTS.md               ★ NEW: 演示用引导文件
├── SOUL.md                 ★ NEW: 演示用引导文件
├── USER.md                 ★ NEW: 演示用引导文件
└── step6.md
```

## ContextBuilder API

```python
@dataclass
class ContextBuilder:
    workspace: str = "."
    bootstrap_files: list[str] = field(
        default_factory=lambda: ["AGENTS.md", "SOUL.md", "USER.md"]
    )

    def build_system_prompt(
        self, identity: str | None = None
    ) -> str:
        ...

    def build_messages(
        self,
        current_message: str,
        history: list[dict] | None = None,
        identity: str | None = None,
    ) -> list[dict]:
        ...
```

## System Prompt 组装格式

```
<identity>

---

## AGENTS.md

<file content>

---

## SOUL.md

<file content>

---

## USER.md

<file content>
```

不存在的文件静默跳过，不报错。

## build_messages 流程

```
build_messages("你好", history=[...])
  │
  ├─ build_system_prompt(identity)  →  "You are nanobot..."
  │
  ├─ [{"role": "system", "content": "You are nanobot..."}]
  ├─ +history (浅拷贝)
  └─ +{"role": "user", "content": "你好"}
       │
       ▼
  [system, *history, user]
```

## 集成方式（方案 A）

AgentRunner 不感知 ContextBuilder：

```python
context = ContextBuilder(workspace=".")
spec = AgentRunSpec(
    initial_messages=context.build_messages("你好", history=history),
    tools=registry,
    provider=provider,
)
result = await AgentRunner().run(spec)
```

## 与 nanobot 对比

| 功能 | nanobot | step6 |
|---|---|---|
| Identity 渲染 | Jinja2 模板 `identity.md` | 纯字符串参数 |
| Bootstrap files | `_load_bootstrap_files()` | 内联在 `build_system_prompt()` |
| Tool contract | 模板 `tool_contract.md` | 后续步骤 |
| Memory (MEMORY.md) | `get_memory_context()` | 后续步骤 |
| Skills | always-on + summary | 后续步骤 |
| Recent history | `memory/history.jsonl` | 后续步骤 |
| 同角色合并 | `_merge_message_content()` | 暂不实现 |
| Runtime context | `append_runtime_context()` | 后续步骤 |
| 测试数 | - | **11** |

## 测试覆盖（11 个）

| # | 测试 | 场景 |
|---|------|------|
| 1 | `test_default_identity_no_bootstrap` | 无文件，默认 identity |
| 2 | `test_with_agents_md` | 只有 AGENTS.md |
| 3 | `test_all_three_bootstrap` | 三个文件都存在 |
| 4 | `test_custom_identity` | 自定义 identity |
| 5 | `test_nonexistent_bootstrap_ignored` | 目录不存在，静默跳过 |
| 6 | `test_custom_bootstrap_list` | 自定义文件列表 |
| 7 | `test_no_history` | build_messages 无历史 |
| 8 | `test_with_history` | build_messages 有历史 |
| 9 | `test_identity_override_in_build_messages` | build_messages 层面覆写 identity |
| 10 | `test_bootstrap_in_build_messages` | 引导文件出现在 system content |
| 11 | `test_integration_with_runner` | 完整链路：ContextBuilder → AgentRunner |

## 暴露的问题

1. **无同角色合并** — 如果 history 最后一条也是 user 消息，会出现两个连续 user
2. **identity 不可持久化** — 每次从代码传字符串，没有配置化的存储
3. **bootstrap 文件只读一次** — `build_system_prompt()` 每次调用都重新读盘，后续可缓存
4. **无 Token 预算** — 没算 system prompt 占了多少 token

## 下一步

**Step 7：Session 持久化** — `Session(key, messages[])` dataclass + `SessionManager` 读写 JSON 文件。
