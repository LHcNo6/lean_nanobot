# Step 30 — Reasoning + Hook 工厂 + Runner/Provider 健壮性收敛（A7 + A8 + H5）

在 step 29（A11–A14、H8：技能、隐藏历史、取消免疫、统一会话）之上，
按 ROADMAP Step 30 对齐 nanobot 三段能力：推理流式输出、按 turn 的 hook
工厂、LLM 错误语义收敛，并顺势收敛本步引入的流式回归：

- **A7 Hook 体系升级**：`hook.py` 引入 `AgentTurnHookContext` /
  `AgentTurnHookSpec` / `build_agent_turn_hook` / `AgentProgressHook`（对齐
  nanobot agent hook 的 turn 级组装方式），loop 通过 spec + 回调组装流式
  发布管道；`AgentHook.on_stream_end` 增加 `resuming` 语义；
- **A8 Runner 健壮性**：错误消息定制（arrearage/自定）、refusal /
  content_filter / error 响应丢弃工具调用、usage 估算回退、max_iterations
  定制消息与 finalization；
- **H5 Provider 重试引擎**：`provider.py` 重写为"响应 + 异常"双路重试
  （`_run_with_retry`）、`retry_mode=standard/persistent`、Retry-After 解析、
  429 可重试判定、角色交替强制；
- **流式回归收敛**：runner 每轮响应后 flush（resuming=True）+ 最终收尾
  flush（resuming=False），loop 发布 typed `StreamEndEvent(resuming=…)`，
  legacy 流式测试同步迁移到新契约。

---

## 一、这一阶段解决了什么问题、为什么要这样做

**A7（hook 工厂）**：step29 之前 hook 要么是零散回调、要么靠手工
`StreamPublishingHook` 把流增量搬进 bus，跨 step 复用时要重复拼装。nanobot
用 `AgentTurnHookSpec` 一次声明、`build_agent_turn_hook` 一次组装。lean
照做：spec 承载 turn 级回调（`on_turn_start`、`on_message`、`on_stream_*`、
`on_retry_wait`、tool 回调），loop 的 `_build_agent_spec` 用
`build_agent_turn_hook(spec)` 生成 runner 用的 hook，并把
`_publish_delta` / `_publish_stream_end` 作为回调注入——流的发布责任从
"hook 内部实现"上移为"loop 提供回调、hook 只负责调用时机"。

**A8（runner 健壮性）**：此前错误分支只有"固定文案 + 原样内容"，导致：
arrearage（欠费/配额）提示、refusal 时工具参数残留、usage 缺失时 `tokens: ?`
占位、max_iterations 无定制文案。本步统一收敛：
- `_error_result` 的优先级：`spec.error_message`（自定）> arrearage 文案
  （欠费可被持久化识别）> 原内容；
- 工具调用门控：refusal / content_filter / error 响应一律丢弃 tool_calls，
  重新包装为普通 `stop` 响应，避免把拒绝时残留的参数当真实工具执行；
- usage 估算回退 `_estimate_usage`（`chars//4` 启发式），保证调用链上
  usage 字段非空；
- `spec.max_iterations_message` 可定制 max_iterations 收尾文案。

**H5（provider 重试引擎）**：旧 `chat_with_retry` 只对异常重试，429 响应
文本（`Retry-After`）与"内容即错误"的响应（arrearage）无法触发重试，
重试间隔也是固定退避、无持久重试。本步把 provider.py 整体重写：
- 双路分类：`_run_with_retry` 同时接受 response 与 exception 两路，分别
  用 `is_transient_response` / `_is_retryable_exception` 判定；
- `retry_mode`：`standard`（有限次退避）与 `persistent`（nanobot 语义：
  长延迟上限 `_PERSISTENT_MAX_DELAY=60s`，连续相同错误超
  `_PERSISTENT_IDENTICAL_ERROR_LIMIT=10` 次才放弃，主要用于真实服务下的
  长尾故障等待）；
- Retry-After：`_extract_retry_after_from_headers`（HTTP 头）与
  `_extract_retry_after_from_response`（响应体文本）双路解析，尊重服务端
  给出的等待时长；
- 429 细化：`_is_retryable_429_response`（arrearage 之外可重试）、
  `_is_retryable_429_response` 不覆盖的 429 文本如 quota 走 persistent；
- `_enforce_role_alternation`：注入/续跑场景下相邻同 role 消息合并，
  避免部分供应商 400。

**流式回归收敛（本步引入）**：runner 现按"每轮响应后 flush（resuming=True）
+ 注入判定后收尾 flush（resuming=False）"调用 `on_stream_end`，比旧的
"每轮一次 flush"次数更多；同时 loop 的 `_publish_stream_end` 从发布 legacy
`StreamDeltaEvent(finished=True)` 改为发布 typed `StreamEndEvent(resuming=…)`。
两处变更让 legacy 流式测试（stream_end 计数、`deltas[-1].finished`）断言
失败——不是 bug，而是契约升级，legacy 测试迁移到新契约。

---

## 二、目标与实现

| 目标 | 实现 |
|------|------|
| Turn 级 hook 组装 | `hook.py`：`AgentTurnHookContext` / `AgentTurnHookSpec` / `build_agent_turn_hook`；`AgentProgressHook`（thinking 块剥离、tool_hint、`_on_progress_accepts` 兼容探针）；`CompositeHook` 保留；`AgentHook.on_stream_end(context, *, resuming=False)` 默认实现 |
| loop 接入工厂 | `loop.py:_build_agent_spec` 用 `build_agent_turn_hook(AgentTurnHookSpec(...))`，回调绑定 `_publish_delta` / `_publish_stream_end`；`StreamPublishingHook` 保留供测试 |
| 流收尾 typed 化 | `_publish_stream_end(*, resuming=False, **_)` 发布 typed `StreamEndEvent(resuming=...)`（`outbound_message_for_event`）；manager 已按 `stream_end=True, resuming` 路由 |
| 错误消息定制 | `runner.py`：`AgentRunSpec.error_message` / `max_iterations_message`；`_ARREARAGE_ERROR_MESSAGE` 常量；`_error_result` 优先级 自定 > arrearage > 内容 |
| 工具调用门控 | refusal / content_filter / error 响应丢弃 tool_calls，rewrap 为 `finish_reason="stop"` |
| usage 回退 | `_request_model` 在 usage 缺失时 `_estimate_usage`（chars//4） |
| 双路重试 | `provider.py`：`_run_with_retry(response, exc, ...)`；非瞬态异常立即重抛；瞬态异常转错误响应；耗尽后重抛 `last_exc` |
| retry_mode | `chat_with_retry` / `chat_stream_with_retry` 保留 `retry_config`（兼容）+ 新增 `retry_mode`；`persistent` 上限 60s、相同错误 10 次 |
| Retry-After | `_extract_retry_after_from_headers` / `_extract_retry_after_from_response` / `_to_retry_seconds` |
| 角色交替 | `_enforce_role_alternation` 合并相邻同 role 消息 |
| 退避修正 | off-by-one 修复：`attempt > config.max_retries` 才放弃（原 `+1` 多试一次） |
| 测试 | 新增 `tests/test_runner_robustness.py`（21 个用例：refusal 丢弃、arrearage、定制消息、usage 估算、Retry-After 解析、429 分类、retry_mode、角色交替）；`tests/test_events.py::TestStreamResumingSemantics`（2 个用例）；legacy `test.py` 迁移到新契约（`StreamEndEvent` 断言、stream_end 计数 2/3） |

---

## 三、核心函数 / 类说明

- `hook.py`：
  - `AgentTurnHookSpec` / `build_agent_turn_hook(spec)` — turn 级声明式 hook
    组装，把 loop 回调（`on_delta_cb` / `on_stream_end_cb`）绑定进 hook；
  - `AgentProgressHook` — `_strip_think`（剥离 `think`/`thinking` 块）、
    `_tool_hint`（`[Tool: name(args)]` 提示）、`_on_progress_accepts`
    （探测回调是否接受 kwargs）；
  - `AgentHook.on_stream_end(self, context, *, resuming=False)` — 新增默认
    参数，向后兼容旧签名。
- `runner.py`：
  - `_error_result(spec, error, ...)` — 自定消息 > arrearage 文案 > 原内容；
  - `should_execute_tools(response)`（内部逻辑）— refusal / content_filter /
    error 时丢弃 tool_calls；
  - `_estimate_usage` — `chars // 4` 启发式补 usage。
- `provider.py`：
  - `_run_with_retry(...)` — 重试中枢，`response` + `exc` 双输入分类，统一
    on_retry_wait 通知；
  - `is_transient_response` / `is_arrearage_response` / `_is_retryable_429_response`
    — 响应内容分类；
  - `_extract_retry_after_from_headers` / `_extract_retry_after_from_response`
    — 服务端等待时长；
  - `_enforce_role_alternation(messages)` — 相邻同 role 合并。
- `loop.py`：
  - `_publish_delta` / `_publish_stream_end` — 流增量 / typed 收尾发布回调；
  - `_build_agent_spec` — 通过 `build_agent_turn_hook` 组装 hook。

---

## 四、暴露的问题 / 取舍

| 取舍 | 说明 | 后续计划 |
|------|------|----------|
| `_estimate_usage` 是启发式 | `chars//4` 只保证字段非空，不精确；真实 provider 均有 usage 时不受影响 | 精度不作保证 |
| `on_stream_end` 触发次数增多 | 每轮响应 flush(resuming=True) + 最终收尾 flush(resuming=False)；契约升级后 legacy 计数测试已迁移 | 消费方按 `resuming` 区分 |
| persistent 模式 60s 上限 | 真实服务长尾等待的折中；单测用 `_sleep_with_heartbeat` 可被打断 | 参数可配置化 |
| `_extract_retry_after` 正则 | 覆盖 `Retry-After: 123` 与带单位的文本；个别供应商格式未覆盖 | 遇到再补 |
| 角色交替只做相邻合并 | 不改写消息顺序，只合并相邻同 role；跨工具结果的历史场景足够 | 视供应商反馈 |

## 五、下一 step 要解决什么

1. step29 遗留：/stop 后 **checkpoint 自动恢复重启**（进程级崩溃恢复后
   自动重发 pending / 检查点）；
2. 隐藏历史的 **public_history_message(s) 展示期移除**（A12 下半场）；
3. pending 队列注入的 checkpoint 语义端到端（多消息连续注入的预算与顺序
   保证）。
