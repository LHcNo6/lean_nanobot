# Step 57: TurnContext 字段重构 + 技术债清理

## 解决了什么问题

step56 的 TurnContext 保留了旧状态机遗留的 `result`/`error`/`summary` 字段：
- `result: AgentRunResult | None` 在 _state_run 中被重建，与扁平字段（final_content/stop_reason 等）重复
- `error: str | None` 是 step32 的临时方案，错误信息已通过 final_content + stop_reason 传递
- `summary: str | None` 与 nanobot 的 `pending_summary` 命名不一致

这些冗余字段增加了维护成本，容易导致数据不一致。

## 原理思路

### 字段移除与替换

| 旧字段 | 新字段 | 说明 |
|--------|--------|------|
| `summary` | `pending_summary` | 对齐 nanobot 命名 |
| `result` | 扁平字段 | final_content/tools_used/all_messages/stop_reason/had_injections |
| `error` | 移除 | 错误通过 final_content + stop_reason 传递 |
| - | `usage: dict[str, int]` | tokens 信息（新增） |
| - | `tools_used: list[str]` | 补充缺失的字段定义 |

### _state_run 简化

- 移除 `ctx.result = AgentRunResult(...)` 重建
- 移除 `ctx.error = ctx.result.error`
- 扁平字段在 runner 返回 tuple 后直接设置（已有逻辑）

### _state_save / _state_respond 改用扁平字段

- `ctx.result is None` → `ctx.final_content is None`（error/tool_reason 时仍保存）
- `ctx.result.final_content` → `ctx.final_content`
- `ctx.result.stop_reason` → `ctx.stop_reason`
- tokens 从 `ctx.usage` 字典获取（_run_agent_loop 暂不填充，默认 0+0）

### _state_compact

- `ctx.summary` → `ctx.pending_summary`

## 核心函数/类

- `loop.py:TurnContext` - 移除 result/error/summary，新增 pending_summary/usage/tools_used
- `loop.py:AgentLoop._state_run` - 不再重建 ctx.result
- `loop.py:AgentLoop._state_save` - 改用 ctx.final_content
- `loop.py:AgentLoop._state_respond` - 改用扁平字段 + ctx.usage
- `loop.py:AgentLoop._state_compact` - ctx.summary → ctx.pending_summary

## 测试结果

- 550 tests，3 个已知环境失败（非回归）
- 新增 6 个测试：
  - TestStep57TurnContextFields：验证 result/error/summary 字段移除、pending_summary/usage/tools_used 字段存在、扁平字段默认值
- 修复了现有测试中 ctx.result/ctx.summary 的引用

## 暴露的问题

- ctx.usage 在 _run_agent_loop 中未被填充（runner 返回的 tuple 不包含 usage），tokens 显示为 0+0。需要后续 step 将 usage 从 runner 传递到 ctx。
- runner.py 中的 IterationContext 仍保留 error 字段（这是 runner 内部迭代上下文，与 TurnContext 无关，不需要修改）。

## 下一 step

step58：runner 收尾对齐（_PERSISTED_MODEL_ERROR_PLACEHOLDER、is_tool_error_result、_merge_message_content、_build_request_kwargs、_append_final_message/_append_model_error_placeholder）。
