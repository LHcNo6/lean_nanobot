# Step 54: 函数式参数 + 流式分段 + background_tasks

## 解决了什么问题

step53 中存在三个与 nanobot 的对齐缺口：

1. **goal_continue_message 是静态字符串**：无法根据 session.metadata 中的目标状态动态生成续跑消息，nanobot 使用闭包动态读取 `goal_state_runtime_lines(session.metadata)`。
2. **_schedule_background 不跟踪后台任务**：`asyncio.create_task()` 创建的任务无人管理，shutdown 时可能丢失（如 consolidate_by_tokens 未完成）。
3. **_wants_stream 无 stream_id 分段**：多段流式响应（工具执行后续跑）无法区分不同段，nanobot 使用 `stream_base_id:stream_segment` 标识每段。

## 原理思路

### 1. goal_continue_message 闭包化

- `AgentRunSpec.goal_continue_message` 类型从 `str | None` 扩展为 `str | Callable[[], str | None] | None`
- runner 的 `_build_goal_continue_message` 检测 callable，调用后取返回值
- loop 的 `_build_agent_spec` 中定义 `_goal_continue()` 闭包，捕获 `session` 变量，动态读取 `session.metadata`
- 闭包调用 `goal_state_runtime_lines()` 获取目标状态行，无活跃目标时返回 None（runner 回退到默认消息）

### 2. _background_tasks 跟踪

- `__init__` 初始化 `self._background_tasks: list[asyncio.Task] = []`
- `_schedule_background` 创建 task 后 append 到列表，并通过 `add_done_callback(self._background_tasks.remove)` 自动清理
- 新增 `close_mcp()` 方法（对齐 nanobot API），shutdown 时 `asyncio.gather(*tasks, return_exceptions=True)` 等待所有后台任务完成
- learn_nano 暂无 MCP 连接，close_mcp 仅做 background tasks drain

### 3. _wants_stream 流式分段

- 在 `_build_agent_spec` 中检查 `msg.metadata.get("_wants_stream")`
- 为 True 时创建 `stream_base_id = f"{session_key}:{time.time_ns()}"` 和 `stream_segment` 计数器
- `_current_stream_id()` 返回 `f"{stream_base_id}:{stream_segment}"`
- 新增 typed event `StreamDeltaEvent`（bus/outbound_events.py），带 `content` 和 `stream_id` 字段
- `_publish_delta` 在 _wants_stream 时走 typed event 路径（带 stream_id），否则保持 legacy StreamDeltaEvent
- `_publish_stream_end` 带 stream_id，resuming=False 时递增 segment

## 核心函数/类

- `runner.py:AgentRunSpec.goal_continue_message`：类型扩展为 `str | Callable[[], str | None] | None`
- `runner.py:AgentRunner._build_goal_continue_message`：callable 检测与调用
- `loop.py:AgentLoop._goal_continue()`：闭包，动态读取 session.metadata 生成目标续跑消息
- `loop.py:AgentLoop._background_tasks`：后台任务跟踪列表
- `loop.py:AgentLoop._schedule_background`：登记 task + 完成自动移除
- `loop.py:AgentLoop.close_mcp`：drain 后台任务
- `bus/outbound_events.py:StreamDeltaEvent`：typed event，带 stream_id
- `loop.py:_publish_delta/_publish_stream_end`：支持 stream_id 分段

## 测试结果

- 514 tests，3 个已知环境失败（openai/Python 版本差异，非回归）
- 新增 10 个测试：
  - TestStep54GoalContinueCallable（3 个）：callable 调用、None 回退、字符串兼容
  - TestStep54BackgroundTasks（3 个）：任务登记、close_mcp drain、完成自动移除
  - TestStep54StreamSegmentation（4 个）：stream_id 递增、typed event 字段、默认值

## 暴露的问题

- PowerShell `-replace` 默认不区分大小写，fork 时 `TestStep53` 和 `Teststep53` 被替换为同名类，导致测试方法被覆盖。已修复类名。
- close_mcp 方法目前只做 background tasks drain，MCP 连接关闭留待后续 harness 阶段。

## 下一 step

step55：ModelRuntimeResolver 完整实现（set_model_preset/set_runtime_model/llm_runtime/provider_signature 热刷新）。
