# Step 29 — 会话记忆可见性 + 隐形续跑 + 取消免疫与并发门控（A11–A14、H8）

在 step 28（A9/A10：运行时上下文 + workspace 绑定）之上，对齐 nanobot 的四段
链路，并把 step 29 阶段引入的若干回归（注入判定竞态、恢复场景持久化边界、
跨会话测试时序）一并收敛：

- **A11 技能加载**：`skills/loader.py` 把内置技能目录装配成可读文本，供
  identity 增强使用（对齐 nanobot `agent/skills.py` 最小集）；
- **A12 会话记忆 + 历史可见性**：`session/history_visibility.py` 的
  HIDDEN_HISTORY_META（subagent 结果等只服务模型上下文，不作为聊天 turn
  展示）；`session/turn_continuation.py` 的隐形续跑策略（sustained goal
  到达 LLM 预算边界时排班续跑消息）与持久化 append 边界；
- **A13 取消免疫与并发门控**：`utils/cancellation.py` 区分"真正取消"与
  "泄漏的 CancelledError"；`/stop` 走 priority 档在消费循环内联执行；
  全局并发门 `_concurrency_gate` 限制跨会话并发 turn 数；
- **A14 会话级任务分派修正**：由 `_active_tasks` 驱动"会话忙 → 消息进
  pending 队列由进行中的 turn 注入"，确定性修复注入竞态。

---

## 一、这一阶段解决了什么问题、为什么要这样做

**A11（skills）**：此前身份/指令纯由 identity 字符串承载，无法引用仓库内
的技能/方法论文档。nanobot 的做法：把技能目录下 `*.md` 装配成
`[Skills]…[/Skills]` 提示块注入 identity 尾部；lean 只做"读目录 → 拼块"
最小实现，不做额外解析。

**A12（历史可见性）**：subagent 结果、检查点恢复行这类消息模型必须看到，
但 UI `/history` 不应展示。nanobot 以 `_hidden_history` 标记表达；同时
`get_history` 不滤隐藏行（它们留在 LLM 上下文里），只影响展示路径。

**A13（取消语义）**：`CancelledError` 可能由外部集成层"假取消"地泄漏进主
消费循环（库内部作用域调用 `task.cancel()`）。若一律 raise，守护进程的主
循环会被噪音杀死；`Task.cancelling() > 0` 才是真取消。配套 `/stop`：此前
命令都要等会话锁，无法打断正在进行的 turn，改走 priority 档在线无锁
执行，取消后 `_dispatch` 把 checkpoint 物化回会话，已完成的工具结果不丢。

**持久化 append 边界演化（回归根因）**：旧实现硬编码 `skip = 2 + len(history)`；
A12 引入 `turn_continuation.prepare_save_boundary` 后，`_save_skip_for_turn`
的"独立未持久化用户消息"分支在**恢复（restore）场景**下多减了 1，把已有
历史行之后的本轮 user 行误算进 append 区间 → 会话出现孤立 `user` 行
（`test_full_loop_save_after_restore_does_not_duplicate` 断言失败）。
修复：仅当 `history_count == 0` 时执行 `initial_count - 1` 偏移，恢复场景
回到 legacy 边界 = `initial_message_count`。

**A14 注入竞态（回归根因）**：`run()` 每消息一个分派任务，同会话第二条
消息的任务可能因 `asyncio.wait_for` 的调度空隙，等到上一 turn 完全结束后
才启动——此时 `lock.locked()` 已为 False，消息被当成一次独立的新 turn 而
不是 mid-turn 注入（`test_per_session_lock` 断言 3 != 4）。修复：分派前先
查 `_active_tasks[key]`，只要存在未完成任务就把消息直接放入该会话的
pending 队列，由进行中的 turn 在 checkpoint 时注入——判定从"时序竞态"
变成"结构性事实"。

**H8（统一会话）**：`config/schema.py` 增加 `unified_session`，所有通道
共享一个会话（单用户多设备），对齐 nanobot。

---

## 二、目标与实现

| 目标 | 实现 |
|------|------|
| 技能注入 | `skills/loader.py`：`SkillLoader.load()` 读取技能目录；`append_skills_block` 包装标记块；loop 在 identity 尾部追加并显示目录清单（`main.py` 装配） |
| 隐藏历史 | `session/history_visibility.py`：`HIDDEN_HISTORY_META` 常量；`is_hidden_history_message`（True 或 dict 标记）；`/history` 展示路径过滤；`get_history` 不滤（对齐 nanobot） |
| 隐形续接续跑 | `session/turn_continuation.py`：`should_persist_user_message` / `internal_continuation_inbound` / `maybe_continue_turn`（预算边界续接策略、轮次计数上限 12）/ `prepare_save_boundary` + `_save_skip_for_turn`（三种 append 形态）|
| 恢复检查点 | `_state_restore`：`RUNTIME_CHECKPOINT_KEY` / `PENDING_USER_TURN_KEY` 原子恢复，崩溃后补齐"Error: Task interrupted…"行 |
| 取消免疫 | `utils/cancellation.py`：`task_is_cancelling()`；`run()` 消费循环对泄漏 CancelledError 记日志继续，真取消才 propagate |
| /stop 在线执行 | `command/builtin.py`：`router.priority("/stop")`；`run()` 对 priority 命令 `_dispatch_command_inline` 无锁内联，`_cancel_active_tasks` 取消并等待 |
| 并发门控 | `loop.py`：`max_concurrent_turns` → `_concurrency_gate`（Semaphore，<=0 不限）；`_dispatch` 在锁内 `async with lock` 限流 |
| 会话忙 → 注入 | `loop.py:run()`：`_active_tasks[key]` 有存活任务 → `_get_or_create_queue(key).put(msg)`；`_dispatch` 内 `_process_message(pending_queue=…)` 在每轮 checkpoint 时把待注入消息并入当前 user 行（runner `_append_injected_messages`，隐藏行不合并）|
| 统一会话 | `config/schema.py` `unified_session`（默认 False）；`loop.py:from_config` 透传，`_effective_session_key` 按配置返回统一 key |
| 回归收敛 | `_save_skip_for_turn` history 分支修复；`test_cross_session_concurrent` 改用 `_consume_final_response` 消费最终响应（此前拿到的是流式 delta，断言时刻 session 尚未落 assistant 行）|

## 三、核心函数 / 类说明

- `utils/cancellation.py: task_is_cancelling()`：判定当前任务是否真被取消
  （`Task.cancelling() > 0`）。
- `session/history_visibility.py: is_hidden_history_message(m)` — 隐藏标记判定
  （True 或 dict）。
- `session/turn_continuation.py`：
  - `maybe_continue_turn(ctx)` — max_iterations 边界时向 pending_queue 排
    隐形续跑消息（`sender_id="system:continuation"`），并抑制响应；
  - `prepare_save_boundary(ctx)` — 计算 `ctx.save_skip`（清续跑簿记 +
    `_save_skip_for_turn`：续跑/skip_user_persist → initial；standalone 未持久化
    → `initial-1`（仅无历史时）；其余 → `initial`）；
  - `_save_skip_for_turn(…)` — 三种 append 形态，历史场景不再误减。
- `loop.py`：
  - `run()` — 消费循环：CancelledError 免疫；priority 命令内联；A14 会话忙
    判定（`_active_tasks`）路由 pending 队列；
  - `_dispatch(msg)` — 会话锁 + 并发门控；`_process_message` 装配
    `pending_queue`、注入回调、rich RequestContext 与 workspace scope；
  - `_cancel_active_tasks(key)` — /stop 取消并等待（含子代理）。
- `command/builtin.py: _cmd_stop` — priority 档；取消后 checkpoint 物化，
  /history 等其余命令仍需会话锁。

## 四、暴露的问题 / 取舍

| 取舍 | 说明 | 后续计划 |
|------|------|----------|
| 注入决策基于任务注册表 | `_active_tasks` 标记删除回调存在极小窗口（任务 done 但回调未跑）——消息可能进 pending 队列而 turn 已无注入机会；极端情况下由下一 turn 的 drain 兜底 | 观察 + 单测覆盖 |
| `get_history` 不滤隐藏行 | 对齐 nanobot（隐藏行留在 LLM 上下文）；若需彻底过滤再做 | 视产品需要 |
| 续跑轮次上限 12 | 防止无限续跑打爆预算；上限后直接收尾响应 | 可配置化 |
| 跨会话并发门控全局 Semaphore | 不区分优先级；长任务占满配额时短任务排队 | 视产品需要改 per-session 配额 |
| `unified_session` 默认关闭 | 与旧行为兼容 | 产品默认开启时再固化 |

## 五、下一 step 要解决什么

1. `/stop` 后 **checkpoint 自动恢复重启**：进程级崩溃恢复（pending / 检查点
  已物化，但缺少"重启后自动重发"路径）；
2. 隐藏历史的 **public_history_message(s) 展示期移除**（A12 下半场）；
3. pending 队列注入的 checkpoint 语义端到端（多消息连续注入的预算与
   顺序保证）。