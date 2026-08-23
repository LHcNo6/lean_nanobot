# Step114 架构设计：exec_session owner_session_key 隔离

## 1. 总体思路

沿用 step113 已注入子代理运行上下文的成果（`current_request_session_key()` 在子代理内返回父
会话 key），在 `ExecSessionManager` 内新增「owner 归属 + 按 owner 过滤」的集中式校验，对齐
nanobot `exec_session.py` 的 `owner_session_key` 机制。工具层（WriteStdinTool /
ListExecSessionsTool）**零改动**，校验在 manager 方法内通过 `current_request_session_key()`
完成，与 nanobot 一致。

## 2. 数据模型改动

- `_ExecSession` 新增字段 `owner_session_key: str | None = None`（构造参数，默认 `None`）。
- `ExecSessionInfo`（列表摘要）新增 `owner_session_key: str | None = None`，供 `list()` 透传。

## 3. 归属打标（`ExecSessionManager.start`）

会话创建后（在 `_sessions[session_id] = session` 之后）立即打标：

```python
session.owner_session_key = current_request_session_key()
```

`current_request_session_key()` 在子代理运行期返回父会话 key（step113 绑定），在顶层/无上下文时
返回 `None`。因此：
- 子代理内创建的会话 → owner = 父 session_key；
- 顶层主代理创建的会话 → owner = 主会话 session_key；
- 无上下文（遗留/测试） → owner = None（全员可见，向后兼容）。

## 4. 按 owner 过滤（集中校验）

所有读/写入口在取会话后，按当前 `session_key` 校验归属。规则（兼顾隔离与兼容）：

- 若会话 `owner_session_key is None` → 对所有观察者可见（兼容遗留无归属会话）；
- 若观察者 `current_request_session_key()` 为 `None` → 不做过滤（无上下文环境见全部，兼容测试）；
- 否则仅当 `session.owner_session_key == 当前 session_key` 时可见；否则对该观察者视为不存在。

落到方法：
- `get(session_id)`：取会话后若 owner 不匹配 → 返回 `None`（视为不存在）。
- `write(...)`：取会话后若 owner 不匹配 → 抛 `KeyError(session_id)`（同「Session not found」语义）。
- `list(filter_owner: str | None = None)`：`filter_owner` 缺省时按 `current_request_session_key()`
  取值；按上述规则过滤。无上下文 → 返回全部。

> 说明：让 `list()` 默认读 `current_request_session_key()`，使 `ListExecSessionsTool` 调用
> `manager.list()` 即自动按当前会话过滤，无需改动工具。

## 5. 模块改动清单

### 5.1 `tools/exec_session.py`
- 新增导入：`from step114.context import current_request_session_key`。
- `_ExecSession.__init__` 增加 `owner_session_key` 形参与字段。
- `ExecSessionManager.start`：创建会话后打标 `owner_session_key`。
- `ExecSessionManager.get`：增加 owner 校验。
- `ExecSessionManager.write`：增加 owner 校验（在取会话后）。
- `ExecSessionManager.list`：增加 `filter_owner` 参数与过滤逻辑。
- `ExecSessionInfo`：增加 `owner_session_key` 字段并填充。

## 6. 数据流示例

```
子代理 A（session_key="parent"）调用 exec
  → start() 打标 owner="parent"
  → list_exec_sessions → manager.list() 读 current_request_session_key()="parent"
    → 仅返回 owner=="parent" 或 owner is None 的会话
子代理 B（session_key="other"）调用 list_exec_sessions
  → manager.list() 读 current_request_session_key()="other"
    → A 的 session（owner="parent"）被过滤，不可见
```

## 7. 利弊与风险

- 利：对齐 nanobot 会话隔离；子代理会话不再跨会话泄漏；集中式校验、工具层零改动。
- 风险/注意：
  - 无 owner 的遗留会话（owner=None）对所有人可见——为兼容 step113 及之前无上下文环境，
    属刻意保留；若未来要求「严格隔离」，可改为 owner=None 仅对 owner=None 观察者可见，但那会
    破坏无上下文测试，故本 step 取兼容策略。
  - `idle_timeout` 自动清理等高级特性不在本 step（step112 已简化）。

## 8. 不在本 step 范围

- cli_app_manager 接线（step115）；
- 子代理 system prompt 模板化（step116）；
- 子代理运行时限制同步（step117）；
- 微压缩工具集对齐（step118）；
- self/my 工具子代理状态可观测（step119）。
