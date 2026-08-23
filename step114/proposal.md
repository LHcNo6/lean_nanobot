# Step114 需求定义：exec_session owner_session_key 隔离

## 1. 问题背景

step113 已把正确的 `session_key` 注入子代理运行上下文（`current_request_session_key()`
在子代理内返回父会话 key）。但 `exec_session.py` 仍是 step112 刻意简化的版本——**完全没有
`owner_session_key` 概念**（见 `exec_session.py:9,497` 注释）。

后果：子代理（或任意会话）创建的「长运行执行会话」对所有调用方一视同仁——`list()` 返回
全部会话、`get/write` 不校验归属。在 nanobot 中，执行会话带 `owner_session_key`，按归属过滤，
子代理创建的会话归属父会话、`list_exec_sessions` 只能看到自己（父会话）的会话，跨会话不可互见。

## 2. 本 step 要解决什么

为长运行执行会话引入 `owner_session_key` 归属与按 owner 过滤，使：
- 子代理创建的会话归属父会话（复用 step113 注入的 session_key）；
- `list_exec_sessions` / `write_stdin` 只能看到/操作自己归属的会话，跨会话不可见；
- 无请求上下文（遗留/顶层无 session_key）的会话保持全员可见，向后兼容。

## 3. 为什么这样做（方案取舍）

- 方案 A「在工具层（WriteStdinTool/ListExecSessionsTool）各自做 owner 判断」：逻辑分散、
  易遗漏、工具签名被污染。**否决**。
- 方案 B（选定）「在 `ExecSessionManager` 内部统一归属校验，方法内直接读
  `current_request_session_key()`」：对齐 nanobot `exec_session.py` 的集中式 owner 校验
  （`exec_session.py:253-254,295` 用 `current_request_session_key()` 判断），工具层零改动、
  签名不变。

## 4. 目标与实现边界（最小增量）

- 目标：执行会话按 owner 隔离；`list/get/write` 在存在 owner 时过滤。
- 边界（**不做**）：
  - 不改 `idle_timeout` 自动清理（nanobot 高级特性，step112/113 已刻意简化）；
  - 不改 `WriteStdinTool` / `ListExecSessionsTool` 对外接口与签名；
  - 不引入新的工具或配置项。

## 5. 验收标准

1. 会话创建时打上 `owner_session_key = current_request_session_key()`（无上下文则为 `None`）。
2. `list(filter_owner)` / 内部按当前 `session_key` 过滤：仅返回 `owner == 当前` 或
   `owner is None` 的会话。
3. `get`/`write` 跨 owner 访问时对该观察者表现为「不存在」（返回 `None` / 抛 `KeyError`）。
4. 无请求上下文环境下行为与 step113 完全一致（全量测试 25 failed 基线不变，无新增回归）。
5. 子代理 A 创建的会话，子代理 B（不同 session_key）经 `list_exec_sessions` 不可见；父会话可见。
