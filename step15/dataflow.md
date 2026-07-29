## Step 15 — Data Flow Documentation (Consolidation + Dream + MemoryStore)

---

## `llm.py`

### Dataclasses (pure data)
- `Runtime`: context_window_tokens, max_tokens, provider, model
- `ToolCallRequest`: id, name, arguments
- `LLMResponse`: content, tool_calls, finish_reason, usage
- `RetryConfig`: max_retries, base_delay, max_delay, retry_mode

---

## `memory.py`

### Constants
- `_RAW_ARCHIVE_MAX_CHARS = 16_000`
- `_ARCHIVE_SUMMARY_MAX_CHARS = 8_000`
- `_HISTORY_ENTRY_HARD_CAP = 64_000`
- `_DEFAULT_MAX_HISTORY = 1000`
- `_DREAM_FILE_EMBED_CAP = 8000`

### `class MemoryStore`
- `__init__(workspace, max_history_entries)`: creates `memory/` dir, sets up file paths
- `append_history(entry, max_chars, session_key)`:
  - Appends JSONL record with cursor, timestamp, content
  - Returns cursor (int)
  - Called by: Consolidator.archive, raw_archive
- `read_unprocessed_history(since_cursor)`:
  - Returns list of entries with cursor > since_cursor
  - Called by: build_dream_prompt
- `raw_archive(messages, max_chars, session_key)`:
  - Formats messages as `[RAW]...` text, appends to history
  - Called by: Consolidator.archive (fallback)
- `compact_history()`:
  - Trims oldest entries when exceeding max_history_entries
- `get_last_dream_cursor()`:
  - Reads `.dream_cursor` file, defaults to 0
  - Called by: build_dream_prompt
- `set_last_dream_cursor(cursor)`:
  - Writes `.dream_cursor` file
  - Called by: AgentLoop.run_dream
- `get_latest_cursor()`:
  - Returns current cursor value from `.cursor` file or last entry
- `build_dream_prompt(max_entries)`:
  - Builds Dream prompt with current memory files + unprocessed history
  - Returns `(prompt_string, last_cursor)` or None
  - Called by: AgentLoop.run_dream
- `_render_current_memory_files()`:
  - Reads SOUL.md, USER.md, memory/MEMORY.md content
  - Truncates each to `_DREAM_FILE_EMBED_CAP`
- `_format_messages(messages)` (static):
  - Formats messages as `[role]\ncontent` text for archive

---

## `consolidation.py`

### Functions
- `_consolidation_boundary(unconsolidated, target_tokens)`:
  - Finds cut point for half-old consolidation
  - Walks from end, keeps messages up to target_tokens

### `class Consolidator`
- `__init__(store, sessions, build_messages, get_tool_definitions, consolidation_ratio, provider)`
- `get_lock(session_key)`: returns per-session asyncio.Lock
- `maybe_consolidate(session, max_tokens, model)` — backward-compat old API
- `_archive_llm(messages, model)` — old API archiver (uses self.provider)
- `pick_consolidation_boundary(session, tokens_to_remove)`:
  - Returns `(end_idx, removed_tokens)` or None
  - New API: finds boundary by token count rather than budget ratio
- `_full_unconsolidated_history(session)`: returns unconsolidated messages
- `_input_token_budget(runtime)`: `context_window - max_tokens - 1024`
- `_truncate_to_token_budget(text, runtime)`: truncates text to fit budget
- `_persist_last_summary(session, summary)`: stores summary in session metadata
- `archive(messages, *, runtime, session_key, summary_messages)`:
  - Calls `runtime.provider.chat()` to summarize
  - On success: `store.append_history(summary)`
  - On failure: `store.raw_archive(messages)`
  - Called by: maybe_consolidate_by_tokens, compact_idle_session
- `maybe_consolidate_by_tokens(session, *, runtime, replay_max_messages)`:
  - Main new API: multi-round token-budget-driven consolidation
  - Uses `pick_consolidation_boundary` per round
  - Applies per-session lock, acquires fresh session from manager
  - Called by: AgentLoop._state_compact, AgentLoop._state_save (background)
- `compact_idle_session(session_key, *, runtime, max_suffix)`:
  - Archives all but last `max_suffix` unconsolidated messages
  - Resets `last_consolidated = 0`
  - Removes archived messages from session

---

## `loop.py`

### `enum TurnState` — unchanged
### `class TurnContext` — unchanged

### `class AgentLoop`
- `__init__(bus, provider, registry, session_manager, context_builder, memory, identity, replay_budget, hooks)`:
  - **New param: `memory`** (MemoryStore) replaces `consolidator`
  - Creates `self.runtime = Runtime(...)` internally
  - Creates `self.consolidator = Consolidator(store=memory, ...)` internally
- `_schedule_background(coro)`: wraps `asyncio.create_task`
- `run()` / `stop()` — unchanged
- `_get_or_create_queue()` / `_dispatch()` / `_drain_leftover()` — unchanged
- `_state_compact(ctx)`:
  - Calls `self.consolidator.maybe_consolidate_by_tokens(ctx.session, runtime=self.runtime)`
  - Reads `_last_summary` from session metadata → sets ctx.summary
- `_state_build(ctx)` — unchanged
- `_state_run(ctx)` — unchanged
- `_state_save(ctx)`:
  - Saves session
  - **New: schedules background consolidation** via `_schedule_background`
- `_state_respond(ctx)` — unchanged
- `run_dream(tools=None)`:
  - Calls `self.memory.build_dream_prompt(max_entries=20)`
  - If new entries exist, runs `AgentRunner.run(dream_prompt)`
  - Sets `set_last_dream_cursor` on success

---

## `main.py`

### New: `_dream_loop(agent_loop)` background task
- Runs every `_DREAM_INTERVAL_SECONDS` (300s)
- Calls `agent_loop.run_dream()`

### New: `/dream` CLI command
- Manually triggers Dream processing

### Modified initialization
- Creates `MemoryStore` instead of direct `Consolidator`
- Starts `dream_task` alongside `loop_task`

---

## End-to-End Data Flow (with Consolidation + Dream)

```
main.py → main()
  ├─ AgentLoop.run() [loop_task]
  │    └─ consumes bus.inbound → _dispatch → _process_message
  │         └─ _state_compact:
  │              consolidator.maybe_consolidate_by_tokens(session, runtime)
  │                ├─ _input_token_budget → 848
  │                ├─ estimate unconsolidated tokens
  │                ├─ if > budget: multi-round compression
  │                │    └─ archive(chunk, runtime) → LLM summary → store.append_history
  │                └─ _persist_last_summary(session, summary)
  │
  ├─ _dream_loop [dream_task]
  │    └─ every 300s: agent_loop.run_dream()
  │         ├─ memory.build_dream_prompt() → unprocessed archive entries
  │         ├─ if entries: AgentRunner.run(dream_prompt)
  │         └─ memory.set_last_dream_cursor(cursor)
  │
  └─ CLI: /dream, /history, /new, /exit
```

### Key Differences from Step 14

| Aspect | Step 14 | Step 15 |
|--------|---------|---------|
| Consolidation | `maybe_consolidate(session, max_tokens, model)` | `maybe_consolidate_by_tokens(session, *, runtime)` |
| Storage | None | MemoryStore (history.jsonl, .cursor, .dream_cursor) |
| Background work | None | `_state_save` schedules background consolidation; `_dream_loop` |
| CLI commands | /history, /new, /exit | + /dream |
| AgentLoop param | `consolidator` | `memory` (MemoryStore) |
| Provider in Consolidator | constructor arg | via `Runtime.provider` |
