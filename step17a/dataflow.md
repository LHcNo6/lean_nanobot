## Step 16 — Data Flow (Subagents + Sustained Goals)

---

## New Files

### `goal_state.py` — Pure functions

- `parse_goal_state(blob)`: dict/JSON → parsed dict or None
- `sustained_goal_active(metadata)`: checks `goal_state.status == "active"`
- `goal_state_runtime_lines(metadata)`: builds human-readable goal lines

### `subagent.py` — SubagentManager

- `SubagentStatus`: task_id, label, phase, iteration, tool_events, usage, error
- `_SubagentHook`: updates SubagentStatus on after_iteration
- `SubagentManager`:
  - `__init__(bus, provider, tools, max_concurrent_subagents, max_iterations)`
  - `spawn(task, label, origin_channel, origin_chat_id, session_key)`: creates bg task
  - `cancel_by_session(session_key)`: cancels all subagents for a session
  - `get_running_count()` / `get_running_count_by_session(key)`
  - `_run_subagent(...)`: builds subagent messages → AgentRunner.run → announce
  - `_announce(...)`: publishes InboundMessage to bus

### `tools/spawn.py` — SpawnTool

- `name`: "spawn"
- Calls `SubagentManager.spawn()`
- Parameters: `task` (required), `label` (optional)

### `tools/long_task.py` — Goal tools

- `CreateGoalTool`:
  - `name`: "create_goal"
  - Parameters: `objective` (required), `ui_summary` (optional)
  - Writes `session.metadata["goal_state"] = {status:"active", ...}`
- `UpdateGoalTool`:
  - `name`: "update_goal"
  - Parameters: `action` (required: complete/cancel/block/replace), `recap`, `objective`, `ui_summary`
  - Updates goal state in session metadata

---

## Modified Files

### `runner.py` — AgentRunSpec changes

```python
@dataclass
class AgentRunSpec:
    # ... existing fields ...
    goal_active_predicate: Callable[[], bool] | None = None
    goal_continue_message: str | None = None
```

**\_run_loop changes** (after injection check, before return):
```
if spec.goal_active_predicate and spec.goal_active_predicate():
    inject {"role": "user", "content": goal_continue_message}
    continue  # another LLM iteration
```

### `loop.py` — AgentLoop changes

**\_\_init\_\__:**
- Accepts `subagent_manager`
- Creates `CreateGoalTool`, `UpdateGoalTool`, `SpawnTool` instances

**_state_build:**
- Gets `goal_state_runtime_lines(ctx.session.metadata)`
- Appends lines to `identity` string before build_messages

**_state_run:**
- Calls `set_session_key()` on goal tools
- Registers `_spawn_tool`, `_create_goal_tool`, `_update_goal_tool` in registry
- Passes `goal_active_predicate` to AgentRunSpec

### `main.py` — Wiring

- Creates `SubagentManager(bus, provider, tools)`
- Creates `SpawnTool`, `CreateGoalTool`, `UpdateGoalTool`
- Registers them in registry
- Passes `subagent_manager` to AgentLoop

---

## End-to-End Data Flow

### Subagent Flow

```
User: "研究这个仓库"
  → AgentLoop._state_run → LLM decides spawn("分析架构")
    → SpawnTool.execute → SubagentManager.spawn(task)
      → asyncio.create_task(_run_subagent)
      → return "Subagent started (id: abc123)"
  → AgentLoop._state_save → session saved
  → AgentLoop._state_respond → "已启动子 agent..."

  [后台] _run_subagent:
    → messages: [subagent_system, user task]
    → AgentRunner.run(provider, tools)
    → _announce: InboundMessage(sender_id="subagent", content="[结果]")
      → MessageBus.inbound

  [主循环] consume_inbound → _dispatch(msg)
    → AgentLoop._process_message → RESTORE→BUILD→RUN→SAVE→RESPOND
    → Agent 自然回复子 agent 结果
```

### Sustained Goal Flow

```
User: "/goal 实现 feature X"
  → AgentLoop._state_build: identity += "Goal (active): 实现 feature X"
  → AgentLoop._state_run: LLM sees goal → starts working
  → ... max_iterations reached ...
    → _run_loop: goal_active_predicate=True → inject "Continue working..."
    → continue → next iteration
  → LLM continues, eventually calls update_goal(action="complete")
    → UpdateGoalTool.execute → session.metadata["goal_state"]["status"]="completed"
  → Next turn: goal_state_runtime_lines returns [] → no more continuation
```

---

## Key Design Decisions

1. **Subagent via background task**: Simple, no separate process/thread needed
2. **Injection via pending_queue**: Reuses existing mid-turn injection mechanism
3. **Goal state in session metadata**: No separate storage needed
4. **Predicate-based continuation**: `goal_active_predicate` is a lightweight callable checked by runner
5. **Tools registered per-turn**: Goals tools get session_key set before each run
