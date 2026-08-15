# Step 39 — `file_state` ContextVar 绑定

## 解决了什么问题及为什么

step38 已有 `request_context` 和 `workspace_scope` 两个 ContextVar 绑定（在 runner 中），但缺少 `file_state` ContextVar。

nanobot 通过 `file_state` ContextVar 追踪文件读写状态，实现两个功能：
1. **read-before-edit 警告**：编辑文件前检查是否已读取，避免覆盖未查看的内容；
2. **read deduplication**：文件内容未变时跳过重复读取，节省 token。

本 step 对齐 nanobot 设计，新增 `tools/file_state.py` 模块（`FileStates`、`FileStateStore`、ContextVar、bind/reset/current），并在 runner 的 `run` 方法中绑定 `file_state` ContextVar。

## 目标和实现

### 目标
- 新增 `FileStates` 类：记录文件读写状态，支持 check_read / is_unchanged；
- 新增 `FileStateStore` 类：按 session_key 存储 FileStates；
- 新增 `file_state` ContextVar + bind/reset/current 函数；
- runner `run` 方法中绑定 file_state ContextVar（与 request_context / workspace_scope 一致）。

### 实现

#### 1. `tools/file_state.py`（新增，约 250 行）

**核心组件**：

| 组件 | 功能 |
|------|------|
| `ReadState` (dataclass) | 文件读取状态：mtime、offset、limit、content_hash、can_dedup |
| `FileStates` | 单会话文件读写追踪器 |
| `FileStateStore` | 按 session_key 存储 FileStates 的查找表 |
| `_current_file_states` (ContextVar) | 当前 async task 绑定的 FileStates |
| `bind_file_states()` | 绑定 FileStates 到当前 task，返回 token |
| `reset_file_states(token)` | 恢复上一次绑定 |
| `current_file_states(default)` | 获取当前绑定的 FileStates，无则返回 default |

**`FileStates` 核心方法**：
- `record_read(path, offset, limit)`：记录文件已读取
- `record_write(path)`：记录文件已写入（标记不可 dedup）
- `check_read(path) -> str | None`：编辑前检查，返回警告或 None
- `is_unchanged(path, offset, limit) -> bool`：read dedup 判断
- `get(path) -> ReadState | None`：获取原始状态
- `clear()`：清空状态

#### 2. runner.py `run` 方法绑定（step39）

```python
from step39.tools.file_state import FileStates, bind_file_states, reset_file_states

# 在 ws_token 之后绑定
file_state_token = bind_file_states(FileStates())

# ... _run_loop ...

finally:
    reset_file_states(file_state_token)  # 先重置 file_state
    if ws_token: reset_workspace_scope(ws_token)
    reset_request_context(token)
```

- 每次 run 创建独立的 `FileStates()` 实例（不按 session 存储，最小增量）；
- 绑定顺序：request_context → workspace_scope → file_state；
- 重置顺序相反：file_state → workspace_scope → request_context（嵌套正确）。

## 设计决策

### 为什么在 runner 中绑定而不是 _run_agent_loop？

step38 已把 `request_context` 和 `workspace_scope` 绑定放在 runner 中，保持一致性，`file_state` 也在 runner 中绑定。nanobot 放在 `_run_agent_loop` 是因为它没有把绑定提取到 runner。

### 为什么每次 run 独立 FileStates 而不是按 session 存储？

- **最小增量**：不需要修改 `AgentLoop`、`AgentRunSpec`、`_build_agent_spec`；
- step38 没有写文件工具，`file_state` 暂时没有实际消费方；
- `FileStateStore` 类已实现，后续可改为按 session 存储（只需修改绑定位置）。

### 为什么不在工具中使用 file_state？

step38 只有 `read_file` 工具，没有写文件工具。`read-before-edit` 警告和 `read deduplication` 需要写文件工具才能发挥作用。留待后续添加写文件工具时集成。

## 核心函数/类功能说明

| 函数/类 | 位置 | 功能 |
|--------|------|------|
| `FileStates` | tools/file_state.py:38 | 单会话文件读写追踪器 |
| `FileStateStore` | tools/file_state.py:153 | 按 session_key 存储 FileStates |
| `bind_file_states()` | tools/file_state.py:184 | 绑定 FileStates 到当前 task |
| `reset_file_states()` | tools/file_state.py:193 | 恢复上一次绑定 |
| `current_file_states()` | tools/file_state.py:175 | 获取当前绑定的 FileStates |

## 测试

新增 `tests/test_file_state.py`，21 个测试，6 个测试类：

| 测试类 | 测试数 | 覆盖点 |
|--------|--------|--------|
| `TestFileStatesRecordRead` | 3 | record_read 存储状态 / offset-limit / 不存在文件 |
| `TestFileStatesRecordWrite` | 2 | record_write 标记不可 dedup / 不存在文件移除状态 |
| `TestFileStatesCheckRead` | 3 | 未读警告 / 已读未修改 / 已读已修改 |
| `TestFileStatesIsUnchanged` | 4 | 相同参数 / 不同 offset / 写入后 / 未读 |
| `TestFileStateStore` | 5 | 创建 / 复用 / 不同 key / None key / clear |
| `TestContextVarBinding` | 4 | bind-current / reset 恢复 / default / 嵌套绑定 |

全量测试：**417 passed**（step38: 396，新增 21），运行时间 12.02s。

## 暴露了什么问题

1. **FileStates 生命周期**：每次 run 独立实例，不按 session 存储。后续可改为 `FileStateStore.for_session(session_key)`，使同会话跨 turn 共享文件状态。
2. **工具未集成**：`read_file` 工具未调用 `record_read`，写文件工具不存在。后续添加写文件工具时需要集成。
3. **`AgentLoop._file_state_store` 未实现**：nanobot 在 `AgentLoop.__init__` 中创建 `self._file_state_store`，step39 未实现。后续可添加。
4. **content_hash 性能**：每次 `check_read` / `is_unchanged` 都计算文件 SHA-256，大文件可能有性能开销。nanobot 也是这样设计的，暂时接受。

## 下一 step 要解决什么

- **step40**：`turn_scopes` + `hook_factories`（turn 级 context manager + hook 工厂）；
- **step41**：`ephemeral` 模式 + `run_extra_hooks_for_ephemeral`（临时运行模式）；
- **后续**：写文件工具集成 file_state（read-before-edit 警告、read dedup）、`AgentLoop._file_state_store` 按 session 存储。

## 与 nanobot 对齐度

| 维度 | step38 | step39 | nanobot |
|------|--------|--------|---------|
| `file_state.py` 模块 | ❌ | ✅ | ✅ |
| `FileStates` 类 | ❌ | ✅ | ✅ |
| `FileStateStore` 类 | ❌ | ✅ | ✅ |
| file_state ContextVar | ❌ | ✅ | ✅ |
| `bind_file_states` / `reset_file_states` | ❌ | ✅ | ✅ |
| runner 中绑定 file_state | ❌ | ✅ | ✅（_run_agent_loop 中） |
| `AgentLoop._file_state_store` | ❌ | ❌（不做） | ✅ |
| 按 session 存储 FileStates | ❌ | ❌（每次 run 独立） | ✅ |
| 工具中使用 file_state | ❌ | ❌（不做） | ✅ |
| read-before-edit 警告 | ❌ | ❌（不做） | ✅ |
| read deduplication | ❌ | ❌（不做） | ✅ |
