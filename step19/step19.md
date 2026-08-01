# Step 19 — Session System Upgrade

在 Step 18 (ToolLoader & Tool System Upgrade) 基础上，对齐 nanobot 的会话管理架构：base64url 文件名编码、两级缓存、TTL 驱动 AutoCompact、文件上限强制、Pending user turn 崩溃恢复、Fork session。

---

## 设计原则

1. **最小增量** — 只改会话系统（session/autocompact/loop/consolidation），不动 runner/context/tool
2. **别名对齐** — 类/方法名与 nanobot 一致（`_storage_key`、`retain_recent_legal_suffix`、`fork_session_before_user_index`），import 路径 `step18.` → `step19.`
3. **向后兼容** — `AgentLoop` 新增可选参数 `session_ttl_minutes=0`（默认禁用 AutoCompact）；`SessionManager` 新增可选 `max_cached_sessions=128`

---

## 文件变更总览

| 操作 | 文件 | 说明 |
|------|------|------|
| **修改** | `session.py` | base64url 编码 + 两级缓存 + `retain_recent_legal_suffix`/`enforce_file_cap` + `list_sessions` + `fork_session_before_user_index` + legacy 迁移 |
| **新增** | `autocompact.py` | `AutoCompact` 类（移植 nanobot） |
| **修改** | `loop.py` | pending user turn 三件套 + `prepare_session` + `check_expired` 触发 + `enforce_file_cap` |
| **修改** | `consolidation.py` | `compact_idle_session` 改用 probe + `retain_recent_legal_suffix` |
| **修改** | `main.py` | `/new` 用 `invalidate()`；`_session_path` → `_get_session_path` |
| **修改** | `test.py` | 新增 35 个测试 |

---

## 技术方案

### 1. base64url 文件名编码（session.py）

替代 step18 的 `safe_filename(key)` 转义（`:` → `_`，有碰撞风险）：

```python
@staticmethod
def safe_key(key: str) -> str:
    return safe_filename(key.replace(":", "_"))

@staticmethod
def _storage_key(key: str) -> str:
    return base64.urlsafe_b64encode(key.encode()).decode().rstrip("=")

@staticmethod
def _decode_storage_key(stem: str) -> str | None:
    # 恢复 rstrip("=") 掉的 padding 后解码，失败返回 None

def _get_session_path(self, key: str) -> Path:
    return self.sessions_dir / f"{self._storage_key(key)}.jsonl"
```

**碰撞消除**：`"a:b"` → `YTpi` vs `"a_b"` → `YV9i`，互不相同。

**Legacy 迁移**（`_load` 内）：新路径不存在时检查 lossy 旧路径（`safe_key`），读 metadata 行的 `key` 防误迁（`_stored_key_for_path`），`shutil.move` 到新路径。

### 2. 两级缓存（session.py）

```python
self._cache: OrderedDict[str, Session] = OrderedDict()          # hot: LRU 强引用, 128
self._overflow_cache: WeakValueDictionary[str, Session] = WeakValueDictionary()  # evict 保身份

def _remember(self, session):   # pop overflow → 入 hot → move_to_end → 溢出 evict 到 overflow
def _cached(self, key):         # hot 命中 move_to_end；overflow 命中 promote 回 hot
def invalidate(self, key):      # 两 cache 都清（/new、compact 重载用）
```

- `get_or_create` / `save` 都走 `_remember`
- 语义：被外部持引用、被 evict 的 session 身份不变；无引用则被 GC，下次从磁盘重载

### 3. Session 方法：retain_recent_legal_suffix + enforce_file_cap（session.py）

```python
FILE_MAX_MESSAGES = 2000

@dataclass
class RetentionResult:
    dropped: list[dict]
    already_consolidated_count: int

class Session:
    def __post_init__(self): ...  # 钳制越界 last_consolidated（防坏 metadata 隐藏全部历史）
    def clear(self): ...
    def retain_recent_legal_suffix(self, max_messages, *, extend_to_user=False) -> RetentionResult
        # 保留合法后缀：锚定 user turn → find_legal_message_start 修掉孤儿 tool 结果
        # 硬上限裁剪；identity 比较算 dropped / already_consolidated_count；原位变更
    def enforce_file_cap(self, on_archive=None, limit=FILE_MAX_MESSAGES)
        # 超限 → retain_recent_legal_suffix(limit) → dropped[already_consolidated:] 交给 on_archive
```

### 4. AutoCompact（新文件 autocompact.py，移植 nanobot）

```python
class AutoCompact:
    _RECENT_SUFFIX_MESSAGES = 8
    _INTERNAL_SESSION_PREFIXES = ("dream:",)   # run_dream 内部会话跳过

    def __init__(self, sessions, consolidator, session_ttl_minutes=0):
        self._ttl ...; self._archiving: set[str]; self._summaries: dict[str, tuple[str, datetime]]

    def _is_expired(self, ts, now=None) -> bool            # ttl<=0 或无 ts → False
    def _has_compactable_idle_tail(self, key) -> bool      # probe retain_recent_legal_suffix(8, extend_to_user=True) 有可丢
    def check_expired(self, schedule_background, resolve_runtime, active_session_keys=()):
        # list_sessions() 遍历 → 跳过 internal/archiving/active → expired+compactable → 入 _archiving → schedule_background(_archive)
    async def _archive(self, key, *, runtime):             # consolidator.compact_idle_session(key, runtime, max_suffix=8)
                                                            # 成功后从 metadata._last_summary 记录到 _summaries
    def prepare_session(self, session, key) -> tuple[Session, str | None]:
        # dream: 直通 → None；archiving/过期 → get_or_create 重载；_summaries 命中（热路径）→ 格式化摘要
        # metadata._last_summary（冷路径/进程重启）→ "Previous conversation summary (last active ...):\n{text}"
```

**依赖**：`check_expired` 需要 `SessionManager.list_sessions()`（step18 没有，新增）：

```python
def list_sessions(self) -> list[dict]:
    # glob("*.jsonl") → _decode_storage_key（fallback: stem.replace("_", ":", 1)）
    # 读 metadata 行 + 首个 user 消息 preview（无则首个非空文本）
    # 按 updated_at 降序
```

### 5. Pending user turn restore（loop.py）

用户消息在 `_state_build`（history 计算之后、build_messages 之前）预存落盘，崩溃时消息已持久化：

```python
_PENDING_USER_TURN_KEY = "pending_user_turn"

def _mark_pending_user_turn(self, session) -> None:   # metadata[KEY] = True
def _clear_pending_user_turn(self, session) -> None:  # pop
def _restore_pending_user_turn(self, session) -> bool:
    # 有 flag 且最后一条是 user → append assistant "Error: Task interrupted before a response was generated."
    # 清 flag，返回 True

# _state_restore 末尾:
if self._restore_pending_user_turn(ctx.session):
    self.sessions.save(ctx.session)

# _state_build（history 之后）:
ctx.session.add_message("user", ctx.msg.content)
self._mark_pending_user_turn(ctx.session)
self.sessions.save(ctx.session)

# _state_save:
skip = 2 + len(ctx.history)                    # 原 1+；多跳过已预存的 user 消息
ctx.session.import_messages(ctx.result.messages[skip:])
self._clear_pending_user_turn(ctx.session)
ctx.session.enforce_file_cap(
    on_archive=lambda chunk: self.memory.raw_archive(chunk, session_key=ctx.session_key)
)
self.sessions.save(ctx.session)
```

**为什么不用 `_state_restore` 预存**：会让 `get_history` 把当前用户消息算进 history → `initial_messages` 中 history 与 `current_message` 重复（step18 的 ContextBuilder 不去重），且破坏 `test_state_build` 的 `len(ctx.history)==1` 断言。

### 6. Fork session（session.py）

```python
_FORK_VOLATILE_METADATA_KEYS = {"goal_state", "pending_user_turn", "_goal_continuation_rounds"}

def fork_session_before_user_index(self, source_key, target_key, before_user_index) -> Session | None:
    # before_user_index 是全局 user 消息序号（0 = 第一个 user 消息之前，n = 第 n+1 个之前）
    # 遍历 source.messages，遇第 N 条 user 消息即停，之前消息 deepcopy 入 copied
    # metadata: deepcopy 后 pop volatile keys；last_consolidated = min(src.lc, len(copied))
    #   src.lc > len(copied) 时 → pop "_last_summary" + lc = 0
    # save(target, fsync=True) 并返回
```

### 7. Loop / Consolidation 集成

- **`AgentLoop.__init__`**：新增 `session_ttl_minutes: int = 0`；构造 `self.auto_compact = AutoCompact(session_manager, self.consolidator, session_ttl_minutes)`
- **`run()`**：消息到达时触发（用户决策：不做 wait_for 轮询）：

```python
while self.running:
    msg = await self.bus.consume_inbound()
    self.auto_compact.check_expired(
        self._schedule_background,
        lambda: self.runtime,
        active_session_keys=set(self._pending_queues),
    )
    asyncio.create_task(self._dispatch(msg))
```

- **`_state_compact`**：`prepare_session` 返回的摘要优先；若本次运行产生新 `_last_summary` 则兜底读取（保持 step18 行为）：

```python
ctx.session, pending = self.auto_compact.prepare_session(ctx.session, ctx.session_key)
ctx.summary = pending
await self.consolidator.maybe_consolidate_by_tokens(ctx.session, runtime=self.runtime)
if ctx.summary is None:
    meta = ctx.session.metadata.get("_last_summary")
    ctx.summary = meta.get("text") if isinstance(meta, dict) else None
```

- **`Consolidator.compact_idle_session`**（consolidation.py）：对齐 nanobot memory.py：`invalidate` → `get_or_create`（强制重载）→ probe `Session` 调 `retain_recent_legal_suffix(8, extend_to_user=True)` → `archive(removed, summary_messages=全部)` → 保留 suffix。同时删掉无用的 `safe_filename` import。

---

## 测试计划（35 个新增测试，总 270）

| 测试类 | 数 | 覆盖 |
|--------|----|------|
| `TestStorageKeyEncoding` | 5 | base64url roundtrip（含 unicode）、碰撞消除、`safe_key`、garbage 解码、legacy 迁移 |
| `TestTwoLevelCache` | 4 | hot 命中身份、evict 到 overflow 保身份、GC 后磁盘重载、`invalidate` |
| `TestRetentionSuffix` | 5 | 基础裁剪、`extend_to_user`、孤儿 tool 结果修剪、已合并前缀计数、`clear` |
| `TestEnforceFileCap` | 3 | 未超限 no-op、超限裁剪 + `on_archive` 回调、已合并前缀不入 archive |
| `TestAutoCompact` | 6 | `_is_expired`、`_has_compactable_idle_tail`、跳过 active/internal、调度 + 实际压缩、`prepare_session` 冷路径、internal/clean 直通 |
| `TestPendingUserTurn` | 4 | mark/clear、restore 追加错误消息、无 flag no-op、非 user 尾部只清 flag |
| `TestForkSession` | 4 | 索引 0/1 前缀、volatile metadata 剥离 + lc 钳制、落盘可重载、越界返回 None |
| `TestListSessions` | 2 | key 解码 + updated_at 降序 + preview、空目录 |
| `TestStep19Integration` | 2 | 崩溃恢复端到端（新 loop 实例恢复）、TTL 自动压缩端到端 |

---

## 预估工作量

| 文件 | 新增 | 修改 | 净增行 |
|------|------|------|--------|
| `session.py` | — | ~+210 | +210 |
| `autocompact.py` | ~120 | — | +120 |
| `loop.py` | — | ~+55 | +55 |
| `consolidation.py` | — | ~±15 | +10 |
| `main.py` | — | ~-3 | -3 |
| `test.py` | — | ~+280 | +280 |
| **总计** | | | **~670** |

---

## 不做事项（推迟到后续步骤）

| 功能 | 原因 | 计划步骤 |
|------|------|----------|
| `_repair` 损坏文件恢复 | 独立可靠性专项 | 后续 |
| `delete_session` / `read_session_file` / `read_session_metadata` / `flush_all` | step19 无消费者 | step23 (HTTP API) |
| `_restore_runtime_checkpoint`（工具调用级恢复） | 复杂度高，仅做 pending user turn | 后续 |
| `recent_message_start_index` 历史切片优化 | `get_history` 行为变更风险 | 后续 |
| Pydantic config（`sessionTtlMinutes`） | 无 config 系统 | step22 |
| `check_expired` 空闲周期轮询 | 用户决策：仅消息到达时检查 | 视需要 |
