# Step 7 — Session 持久化

## 目标

把 step6 的无状态对话升级为有状态持久化：Session 数据类 + SessionManager JSONL 文件存储，实现跨轮次记忆。

## 文件结构

```
step7/
├── __init__.py
├── llm.py, provider.py, openai_compat_provider.py     # from step6
├── tool.py, tools/echo.py                              # from step6
├── runner.py, context.py                                # from step6
├── session.py                ★ NEW: Session + SessionManager
├── main.py                   ★ NEW: 多轮交互 CLI
├── test.py                   ★ NEW: 16 个测试
└── step7.md
```

## Session 数据类

```python
@dataclass
class Session:
    key: str                           # 唯一标识（如 "user:123"）
    messages: list[dict]               # 消息列表
    created_at: str                    # ISO 时间戳
    updated_at: str                    # ISO 时间戳
    metadata: dict                     # 扩展元数据
    last_consolidated: int = 0         # 已归档消息指针（step8 使用）

    def add_message(self, role, content, **kwargs) -> dict
    def get_history(self, max_messages=50) -> list[dict]
```

- `add_message()` 追加 `{role, content, timestamp, **kwargs}` 格式消息
- `get_history()` 返回 `last_consolidated` 之后的消息，按 `max_messages` 从尾部切片

## SessionManager

```python
class SessionManager:
    def __init__(self, workspace: str = ".")
    def get_or_create(self, key: str) -> Session   # 缓存 → 加载 → 新建
    def save(self, session, *, fsync=False) -> None # 原子写入
```

- **缓存**：简单 `dict[str, Session]`
- **文件路径**：`{workspace}/sessions/{safe_filename(key)}.jsonl`
- **safe_filename**：`<>:"/\\|?*` → `_`（对齐 nanobot）
- **JSONL 格式**：

```
{"_type": "metadata", "key": "default", "created_at": "...", "updated_at": "...", "metadata": {}, "last_consolidated": 0}
{"role": "user", "content": "Hello", "timestamp": "..."}
{"role": "assistant", "content": "Hi!", "timestamp": "..."}
```

- **原子写入**：写 `.jsonl.tmp` → `os.replace()` → 清理 tmp
- 支持 `fsync=True`（关机时保证写穿）

## 集成流程

核心原则：**只保存当前轮新增的消息，不重复已有 history**。

```python
session = sm.get_or_create("demo")
history = session.get_history(max_messages=20)  # 已有消息

# 构建本轮 context
messages = ctx.build_messages(current_msg, history=history)
spec = AgentRunSpec(initial_messages=messages, ...)
result = await AgentRunner().run(spec)

# result.messages = [system, *history, user, assistant, tool...]
# 新消息从 history 之后开始
skip = 1 + len(history)   # 1=system, len(history)=已有历史
for m in result.messages[skip:]:
    session.messages.append(m)
sm.save(session)
```

验证（两轮后）：
```
Turn 1: history=[],   skip=1, saved=[user("Hi"), assistant("Hello")]
Turn 2: history=[2],  skip=3, saved=[user("Again"), assistant("Sure")]

磁盘上的 session.messages = [user, asst, user, asst]  ✓
```

## 与 nanobot 对比

| 特性 | nanobot | step7 |
|---|---|---|
| 存储格式 | JSONL | JSONL |
| 缓存 | OrderedDict(128) + WeakValueDictionary | 简单 dict |
| 原子写入 | tmp + os.replace + dir fsync | tmp + os.replace + dir fsync |
| safe_filename | base64 url-safe | `<>:"/\\|?*` → `_` |
| get_history | max_messages + max_tokens + 多种边界处理 | 切片 + max_messages |
| 文件分割 | `_repair()` 恢复损坏 | 返回空 session |
| 遗留兼容 | 3 种 fallback 路径 | 无 |

## 测试覆盖（16 个）

| # | 测试 | 场景 |
|---|------|------|
| 1 | `test_replaces_unsafe_chars` | safe_filename 替换非法字符 |
| 2 | `test_add_message` | 追加消息并加 timestamp |
| 3 | `test_get_history` | 返回全部消息 |
| 4 | `test_get_history_max_messages` | max_messages 限制 |
| 5 | `test_get_history_with_last_consolidated` | last_consolidated 指针生效 |
| 6 | `test_get_or_create_new` | 新 key 返回空 session |
| 7 | `test_save_and_reload` | 写 → 重新加载 → 内容一致 |
| 8 | `test_save_appends` | 追加保存 |
| 9 | `test_cache_hit` | 同一 key 返回同一对象 |
| 10 | `test_corrupt_file_returns_new_session` | 损坏文件不崩溃 |
| 11 | `test_safe_filename_in_path` | 文件路径使用 safe_filename |
| 12 | `test_tmp_file_cleaned_on_save_error` | 错误后 tmp 文件清理 |
| 13 | `test_fsync_flag` | fsync 模式正常工作 |
| 14 | `test_last_consolidated_preserved` | save/load 后值不变 |
| 15 | `test_multi_turn_flow` | 完整两轮 + 重新加载验证持久化 |
| 16 | `test_multi_turn_with_tool_calls` | 工具调用消息也被持久化 |

## 暴露的问题

1. **无 Token 预算** — `get_history()` 只限制条数，不估算 token（step8 做）
2. **无 LRU 缓存驱逐** — session 越多内存占用越高（后续加）
3. **无并发保护** — CLI 场景不需要（后续 HTTP 场景加文件锁）
4. **无自动压缩** — `last_consolidated` 指针已定义但未使用（step8 做）

## 下一步

**Step 8：自动压缩** — `Consolidator.archive()` + `last_consolidated` 指针 + Token 预算控制 `get_history` 切片。
