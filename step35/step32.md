# step32：runner finalization 对齐 — 预算耗尽时的无工具收尾与注入排空

在 step31（A12 下半场：公共历史与运行时上下文展示期移除）之上，
按 ROADMAP 对齐 nanobot 的 runner 核心循环收尾逻辑：

- **max_iterations finalization**：工具调用预算耗尽后，追加一条
  "budget exhausted" 提示，发一次 `tools=None` 的无工具请求，让模型
  基于已有对话和工具结果给出最终答案；失败才用 fallback 文案。
- **error / empty 后注入排空**：LLM error 或空响应重试耗尽后，先
  尝试排空 pending 注入消息，有注入则 continue 让模型看到后续再答，
  无注入才返回 error / empty 结果。
- **governance 异常保护**：`prepare_for_model` 可能因畸形历史消息
  抛异常，此时逐步 strip/repair（placeholder → malformed → orphan →
  backfill），全部失败才用原始 messages，避免一次 governance 失败
  导致整个 turn 崩溃。
- **AgentRunResult 新字段**：增加 `error`（错误文案）和
  `had_injections`（本轮是否消费过注入），供 loop 层事件和续跑决策使用。

本步保持 step31 的"运行时上下文不持久化到 session 历史"设计决策不变，
仅聚焦 runner 循环的收尾健壮性对齐。

---

## 一、这一阶段解决了什么问题、为什么要这样做

**问题 1：max_iterations 时直接给 fallback 文案，浪费了模型的总结能力。**

step31 的 max_iterations 分支在 `finalize_on_max_iterations=True` 时
直接用 `_MAX_ITERATIONS_FALLBACK` 常量作为最终内容，完全不咨询模型。
但实际上模型已经执行了多轮工具调用，掌握了所有工具结果，完全有能力
基于这些信息给出一个有意义的总结。nanobot 的方案是发一次不带工具
定义的请求（`tools=None`），强制模型只能输出文本，用其回答作为最终
答案；只有当这次请求也失败（error / 仍返回 tool_calls / 空内容）时，
才回退到 fallback 文案。

**问题 2：error / empty 后直接返回，忽略了可能已经到达的 pending 注入。**

step31 的 error 分支在 LLM 返回 error 后直接 `return self._error_result(...)`，
empty 分支在重试耗尽后直接发一次额外请求或返回空结果。但此时 pending
queue 中可能已经有用户的新消息（注入），如果直接返回，这些注入会被
丢弃，用户需要重新发送。nanobot 的方案是在 error / empty 后先调用
`_try_drain_injections`，有注入则把注入消息追加到历史并 continue，
让模型在下一次迭代中看到注入内容再回答。

**问题 3：governance 失败导致整个 turn 崩溃。**

`ContextGovernor.prepare_for_model` 会对历史消息做裁剪、占位符清理、
工具调用配对等操作。如果历史消息畸形（比如工具调用 ID 不匹配、空
assistant 消息等），可能抛异常。step31 直接调用，没有异常保护，
一次 governance 失败就会导致整个 turn 崩溃。nanobot 的方案是用
try/except 包裹，失败时逐步调用 repair 方法（strip_placeholder →
strip_malformed → drop_orphan → backfill_missing），全部失败才用
原始 messages 继续。

**问题 4：AgentRunResult 缺少错误和注入信息。**

step31 的 `AgentRunResult` 只有 `final_content` / `stop_reason` 等
字段，没有单独的 `error` 字段（error 时 final_content 就是错误文案，
但调用方无法区分"正常回答"和"错误文案"），也没有 `had_injections`
标记（loop 层无法知道本轮是否消费了注入，影响事件日志和续跑决策）。

---

## 二、目标与实现

| 目标 | 实现 |
|------|------|
| max_iterations 无工具收尾 | `runner.py`：新增 `_BUDGET_EXHAUSTED_FINALIZATION_PROMPT` 常量；新增 `_request_no_tools()` 方法；新增 `_try_finalize_after_max_iterations()` 方法；改造 max_iterations 分支先排空注入再尝试 finalization |
| error 后注入排空 | `runner.py` `_run_loop` error 分支：return 前调用 `_try_drain_injections(phase="after LLM error")`，有注入则 continue |
| empty 后注入排空 | `runner.py` `_run_loop` empty 分支：重试耗尽后不再发额外请求，直接用 `_EMPTY_FINAL_RESPONSE_MESSAGE`，并调用 `_try_drain_injections(phase="after empty response")` |
| governance 异常保护 | `runner.py` `_run_loop` 循环开头：try/except 包裹 `prepare_for_model`，失败时逐步调用 `strip_placeholder_assistant_messages` → `strip_malformed_tool_calls` → `drop_orphan_tool_results` → `backfill_missing_tool_results`，全部失败用原始 messages |
| AgentRunResult 新字段 | `runner.py`：`AgentRunResult` 增加 `error: str \| None = None` 和 `had_injections: bool = False`；`_error_result` 增加 `had_injections` 参数；循环内所有 return 传递 `had_injections` |
| loop 层适配 | `loop.py`：`TurnContext` 增加 `error` / `had_injections` 属性；`_state_run` 从 result 读取并落盘到 ctx |
| 测试覆盖 | `tests/test_runner_finalization.py`：15 个用例，覆盖 finalization 成功/error/tool_calls/关闭开关、error/empty 注入排空、governance 异常保护、新字段默认值与设置 |

---

## 三、核心函数/类功能说明

### `runner.AgentRunner._request_no_tools(spec, messages, hook, iter_ctx) -> LLMResponse`

发送不带工具定义的请求（`tools_defs=None`），强制模型只能输出文本。
用于 max_iterations finalization：工具预算已耗尽，让模型基于已有对话
直接总结，不再允许调用工具。

### `runner.AgentRunner._try_finalize_after_max_iterations(spec, hook, messages, total_usage) -> str | None`

max_iterations 时尝试一次无工具请求让模型给出最终答案。

1. 构建 `finalization_messages = list(messages) + [{"role": "user", "content": _BUDGET_EXHAUSTED_FINALIZATION_PROMPT}]`；
2. 调用 `_request_no_tools` 发请求；
3. 如果 `finish_reason == "error"` → 返回 None；
4. 如果 `has_tool_calls`（模型在无工具请求中仍返回工具调用）→ 返回 None（warning 日志）；
5. 如果内容为空 → 返回 None；
6. 成功：把 finalization 提示和模型回答写入 `messages`（持久化），返回 clean 文本。

### `runner._run_loop` 中的注入排空

在以下 5 个位置调用 `_try_drain_injections`，有注入则设置 `had_injections = True` 并 continue：

1. **error 分支**（`phase="after LLM error"`）：LLM 返回 error 后，先排空注入再决定是否返回 error；
2. **tool execution 后**（`phase="after tool execution"`）：工具执行完后，排空注入再继续；
3. **empty 分支**（`phase="after empty response"`）：空响应重试耗尽后，排空注入再决定是否返回 empty；
4. **final response 后**（`phase="after final response"`，`allow_goal_continue=True`）：模型给出最终回答后，排空注入或 goal_continue；
5. **max_iterations 边界**（`phase="after max_iterations"`）：循环结束后，排空剩余注入再尝试 finalization。

### `runner._run_loop` 中的 governance 异常保护

循环开头用 try/except 包裹 `_GOVERNOR.prepare_for_model(...)`：

1. 成功 → 用 governance 处理后的 `messages_for_model` 发请求；
2. 失败 → 逐步尝试 repair：
   - `ContextGovernor.strip_placeholder_assistant_messages(messages)`
   - `ContextGovernor.strip_malformed_tool_calls(messages)`
   - `ContextGovernor.drop_orphan_tool_results(messages)`
   - `ContextGovernor.backfill_missing_tool_results(messages)`
3. 每步 repair 后再尝试 `prepare_for_model`，成功则用 repair 后的结果；
4. 全部失败 → 用原始 `messages` 作为 `messages_for_model`（不修改持久化的 `messages`）。

注意：governance 修改的是给模型的消息（`messages_for_model`），持久化
用 `messages`。所有 LLM 请求用 `messages_for_model`，所有 append 操作用 `messages`。

### `runner.AgentRunResult` 新字段

- `error: str | None = None`：错误文案。error 终止时设置为错误内容，
  empty_final_response 时设置为 `_EMPTY_FINAL_RESPONSE_MESSAGE`，
  正常终止时为 None。
- `had_injections: bool = False`：本轮是否消费过注入消息。循环级别
  跟踪，任何一次 `_try_drain_injections` 返回 `should_continue=True`
  都会设置为 True。

### `loop.TurnContext` 新属性

- `error: str | None = None`：从 `AgentRunResult.error` 读取；
- `had_injections: bool = False`：从 `AgentRunResult.had_injections` 读取。

`_state_run` 在 `ctx.result = await self._runner.run(spec)` 后读取这两个
字段并落盘到 ctx，供 `_state_respond` 和事件使用。

---

## 四、暴露了什么问题

1. **empty 分支行为变更**：step32 去掉了 step31 的"空响应重试耗尽后
   发 `_EMPTY_RETRY_FINAL_MESSAGE` 额外请求"逻辑，改为对齐 nanobot——
   直接用 `_EMPTY_FINAL_RESPONSE_MESSAGE` 作为最终内容。这是行为
   变更，但 282 个原有测试全通过，说明没有依赖旧行为的测试。

2. **max_iterations 结构差异**：nanobot 的 max_iterations 检查在
   **循环内**（`iteration == spec.max_iterations - 1`），如果有注入
   则 continue（但 continue 后循环结束）。step32 保持 step31 的
   **循环外**处理结构，在循环结束后先排空注入再尝试 finalization。
   这意味着如果在最后一次迭代中有注入，step32 会在循环外排空并尝试
   finalization，而 nanobot 会在循环内排空后 continue（循环结束，不
   做 finalization）。两者行为略有差异，但 step32 的方案更合理（排空
   后再 finalization）。

3. **`_try_finalize_after_max_iterations` 未调用 `hook.finalize_content`**：
   nanobot 在 finalization 请求成功后会调用 `hook.finalize_content(context,
   response.content)` 清理内容（比如去除 markdown 代码块标记等）。
   step32 尚未实现此调用，可考虑后续补充。

4. **`had_injections` 只在 runner 循环级别跟踪**：loop 层的
   `maybe_continue_turn` 等续跑决策尚未使用 `had_injections` 字段，
   当前仅落盘到 ctx 供事件日志使用。未来可基于此字段优化续跑策略。

5. **governance repair 链是顺序尝试而非组合**：当前 repair 链是
   strip_placeholder → strip_malformed → drop_orphan → backfill_missing，
   每步返回新列表后再尝试 `prepare_for_model`。如果某步 repair 成功
   但 `prepare_for_model` 仍失败，会继续下一步 repair（基于上一步
   repair 后的结果）。这与 nanobot 的行为一致，但可能导致多次 repair
   叠加。

---

## 五、下一 step 要解决什么

1. **session get_history 增强（S1-S3）**：对齐 nanobot 的 `get_history`
   增强——token 预算后 user turn 对齐（避免在 assistant 中间截断）、
   空 assistant 消息过滤、`_command` 消息过滤。这些是展示层和回放层
   的健壮性改进，不影响 runner 核心循环。

2. **consolidation replay overflow 压缩（C1-C3）**：对齐 nanobot 的
   replay overflow 压缩——当回放历史超过 token 预算时，自动压缩
   早期消息为摘要，而不是简单截断。涉及 `replay_max_messages` 参数、
   `estimate_session_prompt_tokens` 方法、replay overflow 处理逻辑。

3. **`hook.finalize_content` 调用补充**：在 `_try_finalize_after_max_iterations`
   成功后调用 `hook.finalize_content`，与 nanobot 行为对齐。

4. **运行时上下文持久化策略评估**：step31/32 保持"不持久化"决策，
   但 marker 基础设施已就绪。需评估是否改变决策——若持久化，则
   `loop._state_build` 需把包含运行时上下文的内容 + marker 一起写入
   session，同时 `get_history()` 需在回放前调用 `public_history_message`
   避免重复追加。

5. **`_run_agent_loop` 提取为独立方法**：nanobot 把核心循环提取为
   `_run_agent_loop` 方法，`_run_core` 只做准备和收尾。step32 的
   `_run_loop` 仍然是一个大方法，可考虑重构提取（纯重构，无行为变更，
   但回归风险较高，需谨慎）。
