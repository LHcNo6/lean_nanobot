# Step114 接口契约（api-spec）

本文件定义 step114「exec_session owner_session_key 隔离」的对外契约，供实现与测试对齐。

## B1：_ExecSession 归属字段

`_ExecSession` 新增构造参数与字段：

```python
owner_session_key: str | None = None
```

由 `ExecSessionManager.start` 在创建会话后打标：

```python
session.owner_session_key = current_request_session_key()
```

- 子代理内创建 → `owner == 父 session_key`（step113 注入）；
- 顶层/无上下文 → `owner is None`。

## B2：ExecSessionInfo 摘要字段

`ExecSessionInfo` 新增字段 `owner_session_key: str | None = None`，`list()` 填充之。

## B3：ExecSessionManager 过滤契约

所有读/写入口按当前 `current_request_session_key()` 校验归属，规则：

| 会话 owner | 观察者 session_key | 可见性 |
| --- | --- | --- |
| `None` | 任意 / `None` | 可见（兼容遗留） |
| `X` | `X` | 可见 |
| `X` | `Y` (Y≠X) | 不可见（视为不存在） |
| 任意 | `None`（无上下文） | 可见（兼容测试/顶层） |

落到方法：

- `get(session_id) -> _ExecSession | None`：owner 不匹配当前会话 → 返回 `None`。
- `write(...) -> SessionPoll`：owner 不匹配当前会话 → 抛 `KeyError(session_id)`。
- `list(filter_owner: str | None = None) -> list[ExecSessionInfo]`：
  - `filter_owner` 缺省时取 `current_request_session_key()`；
  - 按 B3 规则过滤后返回，按 `session_id` 排序。

## B4：工具层接口不变

`WriteStdinTool` 与 `ListExecSessionsTool` 的对外签名、参数、返回均不变；
`manager.write` / `manager.list` 内部完成 owner 校验，工具无感知。

## 测试映射

| 契约 | 测试 |
| --- | --- |
| B1 | `tests/test_exec_session.py::TestExecSessionManager*` 新增 owner 打标断言 |
| B3 | 单测：两 owner 会话，`list(filter_owner=...)` 仅返回匹配项；`get/write` 跨 owner 不可见 |
| B3 | 集成：`list_exec_sessions` 跨 session_key 不可见（子代理 A vs B） |
| B4 | `test_list_exec_sessions.py` 既有用例仍通过（无上下文环境下返回全部） |

> 全部测试使用 mock provider / 构造数据，禁止真实网络与 API 调用。
