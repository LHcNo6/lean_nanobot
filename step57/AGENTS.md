# Step 57: TurnContext 字段重构 + 技术债清理

## 目标

移除 TurnContext 中的旧状态机遗留字段，对齐 nanobot 的扁平字段设计：
1. 移除 `result: AgentRunResult | None`（runner 结果不再挂在 ctx 上）
2. 移除 `error: str | None`（错误通过 final_content + stop_reason 传递）
3. 移除 `summary: str | None`，改用 `pending_summary`
4. _state_run 不再重建 ctx.result
5. _state_save/_state_respond 改用扁平字段

## 最小增量方案

### TurnContext 字段改动
- 移除 `summary`，添加 `pending_summary: str | None = None`
- 移除 `result`
- 移除 `error`
- 添加 `usage: dict[str, int] = field(default_factory=dict)`（tokens 信息）

### loop.py 改动
- _state_restore: ctx.summary → ctx.pending_summary
- _build_initial_messages 调用: ctx.summary → ctx.pending_summary
- _state_run: 移除 ctx.result/ctx.error 重建
- _state_save: ctx.result is None → ctx.final_content is None
- _state_respond: 改用 ctx.final_content/stop_reason，tokens 用 ctx.usage
