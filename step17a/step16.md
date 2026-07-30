## Step 16 — Subagents + Sustained Goals

在 Step 15 (Consolidation + Dream) 基础上，引入**子 agent 异步执行**和**持续目标跟踪**。

---

### 新增文件

| 文件 | 职责 |
|------|------|
| `goal_state.py` | 纯函数模块：解析/检查 session metadata 中的目标状态 |
| `subagent.py` | `SubagentManager` + `SubagentStatus` — 后台子 agent 执行 |
| `tools/spawn.py` | `SpawnTool` — 生成子 agent 的工具 |
| `tools/long_task.py` | `CreateGoalTool` / `UpdateGoalTool` — 目标管理工具 |

### 修改文件

| 文件 | 改动 |
|------|------|
| `runner.py` | `AgentRunSpec` 新增 `goal_active_predicate` + `goal_continue_message`；`_run_loop` 在最终回复前检查目标是否活跃，是则注入续行消息 |
| `loop.py` | `AgentLoop.__init__` 接受 `subagent_manager`；`_state_run` 注册 SpawnTool/CreateGoalTool/UpdateGoalTool，注入 goal 续行 predicate；`_state_build` 将 `goal_state_runtime_lines` 注入 identity |
| `main.py` | 创建 `SubagentManager`，注册新工具 |

---

### 架构

```
main.py
 ├─ SubagentManager(bus, provider, tools)
 │    └─ spawn(task) → asyncio.create_task(_run_subagent)
 │         → AgentRunner.run(独立系统提示 + 工具)
 │         → _announce_result → InboundMessage(sender_id="subagent")
 │              → bus → dispatch → 主 agent 回复
 │
 └─ AgentLoop
      └─ _state_build: identity += goal_state_runtime_lines(session.metadata)
      └─ _state_run: goal tools registered + goal_active_predicate
           └─ AgentRunner._run_loop:
                if goal_active_predicate() and no injections:
                     inject "Continue working..." message → continue
```

#### SubagentManager 工作流

1. `spawn(task)` → 生成唯一 task_id，创建 asyncio Task
2. 后台 `_run_subagent`:
   - 构建独立 messages（subagent 系统提示 + 用户任务）
   - 运行 `AgentRunner.run()`（与主 agent 相同 provider/工具）
   - 完成后调用 `_announce_result`
3. `_announce_result`:
   - 格式化结果文本
   - 发布 `InboundMessage(sender_id="subagent", session_key_override=...)` 到 bus
   - 主 agent 下次 dispatch 时作为用户消息处理
4. `cancel_by_session(session_key)` → 取消某 session 所有子 agent

#### Sustained Goal 工作流

1. 用户或 LLM 调用 `create_goal(objective="...")`:
   - 写入 `session.metadata["goal_state"] = {status:"active", objective:"...", ...}`
2. BUILD 阶段 `goal_state_runtime_lines()` 将目标信息注入 system prompt
3. RUN 阶段每次迭代结束时检查 `goal_active_predicate`:
   - 若目标活跃且无外部注入 → 自动注入"继续工作"消息 → `continue`
   - LLM 可继续使用工具，或调用 `update_goal(action="complete")` 完成目标
4. `update_goal(action="complete"|"cancel"|"block"|"replace")` → 更新/结束目标

---

### 关键设计

#### SubagentManager
- 对 `max_concurrent_subagents` 限流（默认 5）
- 子 agent 使用与主 agent 相同的 `LLMProvider` 和 `ToolRegistry`
- 结果通过 `InboundMessage` 返回，利用已有注入机制
- `SubagentStatus` 追踪执行阶段/工具调用/错误

#### Goal State
- 纯函数设计，不依赖工具或循环逻辑
- `parse_goal_state(blob)` — 支持 dict 和 JSON 字符串
- `sustained_goal_active(metadata)` — 检查 `status == "active"`
- `goal_state_runtime_lines(metadata)` — 生成 "Goal (active): objective..." 行

#### Turn Continuation
- `_run_loop` 在 injection 检查后检查 `goal_active_predicate`
- 注入标准的 "Continue working..." 提示
- LLM 可下一轮调用 `update_goal` 完成/取消/替换目标
- 无最大轮次限制（由 `max_iterations` 兜底）

#### 测试覆盖
- **128 原有测试** + **28 新增测试** = **156 tests pass**

| 测试类 | 测试内容 |
|--------|---------|
| `TestGoalState` (7) | parse_goal_state, sustained_goal_active, runtime_lines, truncation |
| `TestCreateGoalTool` (5) | create, duplicate, empty, no session, with summary |
| `TestUpdateGoalTool` (7) | complete, cancel, block, replace, missing objective, no active goal, invalid action |
| `TestSpawnTool` (2) | no manager, empty task |
| `TestSubagentManager` (4) | running count, cancel, spawn without provider |
| `TestRunnerGoalContinuation` (2) | goal active continues, inactive doesn't |

### 相比 Step 15

| 特性 | Step 15 | Step 16 |
|------|---------|---------|
| Subagent | 不支持 | SubagentManager + SpawnTool（后台异步执行） |
| 持续目标 | 不支持 | CreateGoalTool / UpdateGoalTool + session metadata |
| 目标注入 | 无 | `goal_state_runtime_lines()` → system prompt |
| Turn continuation | 不支持 | `goal_active_predicate` → 自动注入续行消息 |
| 工具数 | EchoTool | EchoTool + SpawnTool + CreateGoalTool + UpdateGoalTool |
| 文件数 | 20 | 25 |
| 测试数 | 128 | 156 |
