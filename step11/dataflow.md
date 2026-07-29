# Data Flow — Step 11: Hook System

## End-to-End Flow

```
User CLI input
  → main() publishes InboundMessage to MessageBus.inbound
    → AgentLoop.run() consumes InboundMessage
      → AgentLoop._dispatch() → per-session lock → _process_message()
        → TurnState machine (same as step10):
          _state_restore → SessionManager.get_or_create()
          _state_compact → Consolidator.maybe_consolidate()
          _state_build   → ContextBuilder.build_messages()
          _state_run     → AgentRunner.run(spec):
            → hook.before_run(AgentRunHookContext)
            → _run_loop():
              → hook.before_iteration(AgentHookContext)
              → Provider.chat_with_retry()
              → ToolRegistry.execute() for tool calls
              → hook.after_iteration(AgentHookContext)
            → hook.after_run(AgentRunHookContext)  [on success]
            → hook.on_error(AgentRunHookContext)     [on exception]
            → hook.on_finally(AgentRunHookContext)   [always]
          _state_save   → Session.import_messages() + save()
          _state_respond → builds OutboundMessage
      → publishes OutboundMessage to bus
  → main() prints response
```

---

## bus.py, events.py, llm.py, provider.py, openai_compat_provider.py

Same as step9/step10.

---

## tool.py, tools/echo.py

Same as step9/step10.

---

## session.py, consolidation.py, context.py

Same as step9/step10.

---

## hook.py (NEW — step11 addition)

### `@dataclass AgentHookContext`
- Fields: `iteration` (int), `messages` (list), `session_key` (str | None), `response`, `usage`, `tool_calls`, `tool_results`, `final_content`, `stop_reason`, `error`
- Created and populated by: `AgentRunner._run_loop()` per iteration
- Passed to: `hook.before_iteration(ctx)`, `hook.after_iteration(ctx)`

### `@dataclass AgentRunHookContext`
- Fields: `messages`, `final_content`, `tools_used`, `usage`, `stop_reason`, `error`, `exception`
- Created by: `AgentRunner.run()` before calling `hook.before_run()`
- Passed to: `hook.before_run()`, `hook.after_run()`, `hook.on_error()`, `hook.on_finally()`

### `class AgentHook`
- Default (no-op) async methods: `before_run()`, `after_run()`, `on_error()`, `on_finally()`, `before_iteration()`, `after_iteration()`
- Subclasses override methods to inject custom behavior (logging, tracing, metrics, etc.)

### `class CompositeHook(AgentHook)`
- `__init__(hooks)`: stores list of `AgentHook` instances
- `_for_each(method, context)`: iterates hooks, calling each hook's method; isolates errors (logs exception, continues)
- Delegates all hook methods to `_for_each`, enabling multiple hooks to run concurrently in sequence

---

## runner.py (modified — now integrates hooks)

### `@dataclass AgentRunSpec`
- Same as step9/step10 but adds: `hook` (AgentHook | None), `session_key` (str | None)

### `@dataclass AgentRunResult`
- Same as step9/step10

### `class AgentRunner`
- `run(spec)`:
  - Input: `AgentRunSpec`
  - Process:
    1. Creates `AgentRunHookContext` from `spec.initial_messages`
    2. Calls `hook.before_run(run_ctx)`
    3. Try: calls `_run_loop()` → `AgentRunResult`
       - On success: populates `run_ctx` with result fields, calls `hook.after_run(run_ctx)`
       - On exception: populates `run_ctx.exception`, calls `hook.on_error(run_ctx)`, re-raises
    4. Finally: always calls `hook.on_finally(run_ctx)`
  - Output: `AgentRunResult`
  - Called by: `AgentLoop._state_run()`

- `_run_loop(spec, messages, tools_used, total_usage, hook)`:
  - Process (same core loop as step9/step10):
    1. For each iteration:
       - Creates `AgentHookContext` with iteration, messages, session_key
       - Calls `hook.before_iteration(iter_ctx)`
       - Calls `spec.provider.chat_with_retry(messages, tools=...)`
       - Updates `iter_ctx` with response, usage
       - If tool_calls: appends assistant msg, executes each tool, appends results, calls `hook.after_iteration()`, continues
       - Else: appends final assistant msg, calls `hook.after_iteration()`, returns result
    2. Max iterations: returns fallback result

---

## loop.py (modified — now passes hooks to runner)

### `class AgentLoop`
- `__init__(bus, provider, registry, session_manager, context_builder, consolidator, identity, replay_budget, hooks=None)`:
  - Same as step10 but stores `self.hooks` list
- `_state_run(ctx)`:
  - Same as step10 but: if hooks exist, creates `CompositeHook` (or single hook); passes hook + session_key to `AgentRunSpec`
  - Data flow: hooks → AgentRunSpec → AgentRunner.run() → hook lifecycle

All other state handlers (`_state_restore`, `_state_compact`, `_state_build`, `_state_save`, `_state_respond`) are identical to step10.

---

## main.py

### `main()`
- Identical to step10. No hooks are passed to `AgentLoop` in the default CLI (hooks are optional).
