# Step 31 — 公共历史与运行时上下文展示期移除（A12 下半场）

在 step 30（A7 + A8 + H5：hook 工厂、runner 健壮性、provider 重试）之上，
按 ROADMAP 对齐 nanobot 的隐藏历史可见性下半场能力：

- **A12 下半场**：`runtime_context.py` 引入 `public_history_message(s)`
  展示期移除函数，基于 `_runtime_context` marker 精确剥离运行时上下文后缀；
  `context.py` 的 `build_messages` 把 marker 持久化到尾部用户消息的 metadata；
  `session/manager.py` 新增 `get_public_history()` 接口（过滤隐藏行 +
  移除运行时上下文）；`consolidation.py` 摘要前自动过滤运行时上下文，
  避免 `[Runtime Context — metadata only...]` 等内部标记污染摘要内容。

本步保持 step30 的"运行时上下文不持久化到 session 历史"设计决策不变——
marker 只存在于内存中的 `initial_messages`。因此 `public_history_message`
对当前历史消息暂无后缀可移除，但函数和接口已完整就绪，待未来持久化策略
启用后自动生效。

---

## 一、这一阶段解决了什么问题、为什么要这样做

**隐藏历史的展示期移除（A12 下半场）**：step29 引入了 `_hidden_history`
标记（`session/history_visibility.py`），把 subagent 内部注入、自动化
turn 等"仅供模型消费、不应作为聊天轮次展示"的消息打上标记。但 step30
只实现了标记本身，没有提供"展示时过滤"的接口——调用方需要自己遍历
历史、判断标记、手动过滤，重复且易错。

nanobot 的方案是两层过滤：
1. `is_hidden_history_message()` — 过滤 `_hidden_history` 标记的消息；
2. `public_history_message()` — 对剩余消息逐条移除运行时上下文后缀
   （基于 `_runtime_context` marker 中的 `suffix` / `blocks` 精确匹配）。

lean 照做，但做了最小增量取舍：
- `public_history_message` / `public_history_messages` 完整实现（纯函数，
  与 nanobot 行为一致）；
- `Session.get_public_history()` 组合两层过滤，返回用户可见的历史副本；
- `Consolidator.archive()` 在摘要前自动调用 `public_history_messages`，
  确保内部标记不进入摘要；
- `build_messages` 把 `append_runtime_context` 返回的 marker 写入尾部
  消息 metadata（此前被丢弃为 `_meta`），为未来持久化策略做好准备。

**为什么不直接持久化运行时上下文到 session 历史**：step30 的设计是
运行时上下文只拼进内存中的 `initial_messages`，session 历史保持原始
用户文本。这样做的好处是：历史回放、摘要、token 估算都不包含运行时
上下文的"噪音"；代价是跨进程恢复后运行时上下文丢失。本步不改变这个
取舍，只把展示期移除的基础设施搭好，待未来评估是否需要持久化时直接
启用即可。

---

## 二、目标与实现

| 目标 | 实现 |
|------|------|
| 运行时上下文展示期移除 | `runtime_context.py`：`public_history_message(message)` / `public_history_messages(messages)`，基于 `RUNTIME_CONTEXT_HISTORY_META` marker 精确移除后缀（文本 `suffix` / 多模态 `blocks` 两种形态） |
| marker 持久化到内存消息 | `context.py`：`build_messages` 把 `append_runtime_context` 返回的 marker 写入尾部用户消息（含角色合并分支），key 为 `_runtime_context` |
| 公共历史接口 | `session/manager.py`：`Session.get_public_history(max_messages, max_tokens)` — 先 `get_history` 取未归档消息，再过滤 `_hidden_history`，最后逐条 `public_history_message` |
| 摘要路径自动过滤 | `consolidation.py`：`archive()` 在格式化前对 `summary_messages`（或 `messages`）调用 `public_history_messages` |
| 测试覆盖 | `tests/test_public_history.py`：18 个用例，覆盖文本/多模态移除、marker 持久化、公共历史过滤、深拷贝不突变等 |

---

## 三、核心函数/类功能说明

### `runtime_context.public_history_message(message) -> dict`

基于 `RUNTIME_CONTEXT_HISTORY_META`（`"_runtime_context"`）marker 精确
移除运行时上下文后缀，返回深拷贝。

- 无 marker 或 marker version != 1 → 原样返回（深拷贝）；
- 文本形态：marker 含 `suffix`，若 `content.endswith("\n\n" + suffix)` 则
  精确剥离；若 `content == suffix`（空原始内容）则置空；
- 多模态形态：marker 含 `blocks`，若 `content[-count:] == blocks` 则精确
  剥离尾部 text 块；
- marker 与实际内容不匹配时不移除（保守策略，避免误删用户内容）。

### `runtime_context.public_history_messages(messages) -> list[dict]`

批量版本，逐条调用 `public_history_message`。

### `context.ContextBuilder.build_messages(...)`

step31 变更：`append_runtime_context` 返回的 `rc_meta` 不再丢弃，而是
写入尾部用户消息的 `_runtime_context` 字段。角色合并分支（历史末尾
同 role 时合并内容）同样写入 marker。

### `session.Session.get_public_history(max_messages=50, max_tokens=0) -> list[dict]`

返回用户可见的历史副本：
1. 调用 `get_history()` 取未归档消息（支持 token 预算）；
2. 过滤 `is_hidden_history_message()` 为真的消息（`_hidden_history` 标记）；
3. 对剩余消息逐条调用 `public_history_message()` 移除运行时上下文；
4. 返回深拷贝，不影响 session 内部存储。

### `consolidation.Consolidator.archive(...)`

step31 变更：在 `_format_messages` 之前，对 `summary_messages`（若提供）
或 `messages` 调用 `public_history_messages()`，确保运行时上下文标记
不进入摘要 prompt。

---

## 四、暴露了什么问题

1. **marker 未持久化到 session 历史**：当前 `loop._state_build` 持久化
   用户消息时用的是原始 `ctx.msg.content`（不含运行时上下文），因此
   `get_public_history()` 对运行时上下文的移除暂无实际效果。隐藏历史
   的过滤已生效。
2. **`get_history()` 与 `get_public_history()` 并存**：调用方需要明确
   区分"给 LLM 用的历史"（`get_history`，含隐藏行和运行时上下文）和
   "给用户展示的历史"（`get_public_history`）。当前 loop 内部统一用
   `get_history`，未来 webui / sdk 层应切换到 `get_public_history`。
3. **Consolidator 摘要过滤是单向的**：`archive()` 只过滤摘要输入，
   不修改 `session.messages`。因此 `get_history()` 仍会返回含运行时
   上下文的消息（如果未来持久化了的话），展示层需自行调用
   `public_history_message`。

---

## 五、下一 step 要解决什么

1. **运行时上下文 marker 持久化到 session 历史**：评估是否改变 step30
   的"不持久化"决策——若持久化，则 `loop._state_build` 需把包含运行时
   上下文的内容 + marker 一起写入 session，同时 `get_history()` 需在
   回放前调用 `public_history_message` 避免重复追加。
2. **checkpoint / pending 注入的端到端测试收敛**：step30 已实现
   checkpoint 恢复和 pending 注入机制，但缺少"注入后 /stop 再恢复"
   的端到端测试，需补充验证注入内容不丢失。
3. **`run()` 消费循环的 `_pending_queues` 判断对齐**：当前 step30 用
   `_active_tasks` 判断是否有正在进行的任务，nanobot 用 `_pending_queues`
   判断。两者判断时机略有不同，需评估是否对齐（有回归风险，需谨慎）。
