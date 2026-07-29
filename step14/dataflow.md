## Step 14 — Data Flow Documentation

**New in Step 14 vs Step 13:** Context governance system. `ContextGovernor` sanitizes and budgets messages before each LLM iteration, handling placeholder stripping, malformed tool calls, orphan tool results, tool result backfill, tool result truncation, inflight compaction, and history snipping.

---

## `llm.py`

### Dataclasses (pure data, no logic)
- `ToolCallRequest`: id, name, arguments
- `LLMResponse`: content, tool_calls, finish_reason, usage
- `RetryConfig`: max_retries, base_delay, max_delay, retry_mode

---

## `events.py`

### Dataclasses (pure data, no logic)
- `InboundMessage`: content, channel, sender_id, chat_id, timestamp, session_key, metadata
- `OutboundMessage`: content, channel, chat_id, metadata
- `StreamDeltaEvent(OutboundMessage)`: content, channel, chat_id, metadata, finished, session_key

---

## `bus.py`

### `class MessageBus`
- `__init__()`: creates two `asyncio.Queue` (inbound, outbound)
- `publish_inbound(msg)`: puts InboundMessage → inbound queue
  - Called by: main.py → main(), loop.py → _drain_leftover
- `consume_inbound()`: gets InboundMessage from inbound queue
  - Called by: loop.py → AgentLoop.run()
- `publish_outbound(msg)`: puts OutboundMessage/StreamDeltaEvent → outbound queue
  - Called by: loop.py → StreamPublishingHook, _dispatch
- `consume_outbound()`: gets OutboundMessage from outbound queue
  - Called by: main.py → main()
- `inbound_size` / `outbound_size`: queue size properties

---

## `helpers.py`

### Functions
- `truncate_text(text, max_chars)`: truncates string with suffix, returns original if within limit
  - Called by: ContextGovernor.normalize_tool_result
- `stringify_text_blocks(content)`: extracts text from content block list, returns joined string or None
- `ensure_nonempty_tool_result(tool_name, content)`: replaces None/empty results with placeholder message
  - Called by: ContextGovernor.normalize_tool_result
- `find_legal_message_start(messages)`: finds first index where tool results have matching tool call declarations
  - Called by: ContextGovernor._legal_history_tail
- `estimate_message_tokens(message)`: estimates token count for a single message dict
  - Called by: estimate_prompt_tokens, ContextGovernor.snip_history
- `estimate_prompt_tokens(messages, tools)`: sums message tokens + tool tokens + overhead
  - Called by: estimate_prompt_tokens_chain
- `estimate_prompt_tokens_chain(provider, model, messages, tools)`: tries provider's counter first, falls back to char estimate
  - Called by: ContextGovernor.compact_inflight_overflow, ContextGovernor.snip_history

---

## `governance.py`

### Constants
- `SNIP_SAFETY_BUFFER`, `MICROCOMPACT_KEEP_RECENT`, `MICROCOMPACT_MIN_CHARS`, `INFLIGHT_COMPACT_TARGET_RATIO`
- `COMPACTABLE_TOOLS`: tools whose results can be compacted mid-flight
- `TOOL_RESULT_OFFLOAD_EXEMPT_TOOLS`: tools exempt from truncation
- `BACKFILL_CONTENT`: placeholder for missing tool results
- `PLACEHOLDER_TEXTS`: assistant messages to strip

### `_tool_call_name_is_valid(tool_call)` (function)
- Validates that tool call dict has a non-empty function name
- Called by: ContextGovernor.strip_malformed_tool_calls

### `@dataclass ContextGovernanceConfig`
- Fields: tools, context_window_tokens, context_block_limit, max_tokens, max_tool_result_chars, workspace, session_key, inflight_start_index

### `class ContextGovernor`
- `prepare_for_model(config, messages, compacted_tool_call_ids)`:
  - Input: config, message list, set of already-compacted tool call IDs
  - Process: chain of cleaning steps → returns sanitized messages
  - Output: cleaned message list
  - Called by: AgentRunner._run_loop
  - Calls: strip_placeholder_assistant_messages, strip_malformed_tool_calls, drop_orphan_tool_results, backfill_missing_tool_results, apply_tool_result_budget, compact_inflight_overflow, snip_history (loops drop_orphan + backfill twice)
- `input_budget(config)` (static):
  - Returns usable token budget from context window minus max_tokens and safety buffer
- `normalize_tool_result(config, tool_call_id, tool_name, result)` (static):
  - Ensures non-empty, truncates if over budget and not exempt
- `strip_placeholder_assistant_messages(messages)` (static):
  - Removes assistant messages with placeholder content (no tool_calls)
- `strip_malformed_tool_calls(messages)` (static):
  - Removes tool calls with invalid names, drops empty tool_call arrays
- `drop_orphan_tool_results(messages)` (static):
  - Removes tool messages whose tool_call_id has no matching assistant declaration
- `backfill_missing_tool_results(messages)` (static):
  - Inserts placeholder tool messages for declared tool calls missing results
- `apply_tool_result_budget(config, messages)`:
  - Applies normalize_tool_result to each tool message
- `compact_inflight_overflow(config, messages, compacted_tool_call_ids)`:
  - If over budget, compacts COMPACTABLE_TOOLS results by replacing with summary text
  - Calls: estimate_prompt_tokens_chain, _apply_recorded_compactions, _inflight_compaction_candidates, _summary_for
- `snip_history(config, messages)`:
  - If over budget, drops oldest non-system messages preserving legal tails
  - Calls: estimate_prompt_tokens_chain, estimate_message_tokens, _legal_history_tail
- `_summary_for(message)` (static):
  - Returns compaction placeholder string
- `_legal_history_tail(kept, non_system)` (static):
  - Ensures history ends at a valid boundary (after user message)
  - Calls: _user_tail, find_legal_message_start
- `_user_tail(messages, last)` (static):
  - Returns suffix starting from last user message
- `_apply_recorded_compactions(messages, compacted_tool_call_ids)` (static):
  - Applies already-recorded compactions to messages
- `_inflight_compaction_candidates(config, messages, compacted_tool_call_ids)`:
  - Returns list of (index, tool_call_id) candidates for compaction, sorted by age

---

## `hook.py`

### Dataclasses
- `AgentHookContext`: iteration, messages, session_key, response, usage, tool_calls, tool_results, final_content, stop_reason, error, stream_content
- `AgentRunHookContext`: messages, final_content, tools_used, usage, stop_reason, error, exception

### `class AgentHook` (abstract base)
- `before_run(ctx)`, `after_run(ctx)`, `on_error(ctx)`, `on_finally(ctx)`, `before_iteration(ctx)`, `after_iteration(ctx)`, `on_stream(ctx, delta)`, `on_stream_end(ctx)`

### `class CompositeHook(AgentHook)`
- Delegates all hook methods to list of hooks with error isolation

---

## `context.py`

### `class ContextBuilder`
- `build_system_prompt(identity, session_summary)`: reads bootstrap files, builds system prompt
  - Called by: build_messages
- `build_messages(current_message, history, identity, session_summary)`: assembles system + history + user message
  - Called by: loop.py → AgentLoop._state_build

---

## `consolidation.py`

### Functions
- `estimate_message_tokens(msg)`: token estimate via string length heuristic
  - Called by: estimate_prompt_tokens, Consolidator._find_boundary, Session.get_history
- `estimate_prompt_tokens(messages)`: sums message tokens + overhead
  - Called by: Consolidator.maybe_consolidate

### `class Consolidator`
- `maybe_consolidate(session, max_tokens, model)`: if unconsolidated exceeds budget, archives/summarizes old messages
  - Called by: loop.py → AgentLoop._state_compact
  - Calls: estimate_prompt_tokens, _find_boundary, _archive
- `_find_boundary(unconsolidated, target_tokens)` (static): finds cut point
- `_archive(messages, model)`: calls provider.chat_with_retry to summarize
  - Calls: _format_messages
- `_format_messages(messages)` (static): formats messages for summarization prompt

---

## `tool.py`

### `class ToolResult(str)`
- `error(content)`: classmethod returning ToolResult with is_error=True

### `class Tool(ABC)`
- `to_schema()`: returns OpenAI-compatible tool schema dict
  - Called by: ToolRegistry.get_definitions

### `class ToolRegistry`
- `register(tool)`, `unregister(name)`, `get(name)`, `has(name)`
- `get_definitions()`: returns list of tool schemas
  - Called by: AgentRunner._run_loop, ContextGovernor.snip_history/compact_inflight_overflow
- `execute(name, **params)`: async, executes tool by name
  - Called by: AgentRunner._run_loop

---

## `tools/echo.py`

### `class EchoTool(Tool)`
- `name`: "echo", `execute(**kwargs)`: returns `ToolResult("Echo: {text}")`

---

## `session.py`

### Functions
- `safe_filename(name)`: replaces unsafe filesystem characters
- `ensure_dir(path)`: creates directory if not exists

### `class Session`
- `add_message(role, content, **kwargs)`: appends message with timestamp
- `import_messages(messages)`: appends messages with timestamp fallback
  - Called by: loop.py → AgentLoop._state_save
- `get_history(max_messages, max_tokens)`: returns unconsolidated messages, trimmed by token/message count
  - Called by: loop.py → AgentLoop._state_build

### `class SessionManager`
- `get_or_create(key)`: returns cached or loaded or new Session
  - Called by: loop.py → AgentLoop._state_restore, main.py → main()
- `_load(key)`: reads JSONL file → reconstructs Session
- `save(session, fsync)`: writes metadata + messages as JSONL via atomic replace
  - Called by: loop.py → AgentLoop._state_save

---

## `provider.py`

### Functions
- `_is_retryable_exception(exc)`: determines if exception is retryable
- `_backoff_delay(attempt, config)`: exponential backoff with jitter

### `class _StreamGuard`
- Simple flag: delta_delivered

### `class LLMProvider(ABC)`
- `chat(messages, ...)`: abstract
- `chat_stream(messages, ...)`: default calls chat(), then on_content_delta
- `chat_with_retry(messages, ...)`: retry loop with exponential backoff
  - Called by: Consolidator._archive
- `chat_stream_with_retry(messages, ...)`: streaming retry, only retries if no delta delivered
  - Called by: AgentRunner._run_loop

---

## `openai_compat_provider.py`

### `class OpenAICompatProvider(LLMProvider)`
- `chat(messages, ...)`: calls OpenAI API → _parse_response
  - Calls: _build_kwargs, _parse_response
- `chat_stream(messages, ...)`: streaming, calls on_content_delta per chunk
  - Calls: _build_kwargs, _assemble_from_chunks
- `_build_kwargs(...)`: builds API kwargs dict
- `_parse_response(resp)`: parses API response → LLMResponse
  - Calls: _parse_tool_calls
- `_assemble_from_chunks(chunks)`: concatenates stream chunks → LLMResponse
- `_parse_tool_calls(tool_calls_raw)` (static): raw → list[ToolCallRequest]
- `from_env()` (classmethod): reads env vars → OpenAICompatProvider
  - Called by: main.py → main()

---

## `runner.py`

### Dataclasses
- `AgentRunSpec`: initial_messages, tools, provider, max_iterations, model, temperature, max_tokens, hook, session_key, injection_callback, **governance_config (NEW)**
- `AgentRunResult`: final_content, messages, tools_used, usage, stop_reason; total_prompt_tokens / total_completion_tokens properties

### `class AgentRunner`
- `run(spec)`:
  - Input: AgentRunSpec
  - Process: hook.before_run → _run_loop → hook.after_run/on_error → hook.on_finally
  - Output: AgentRunResult
  - Called by: loop.py → AgentLoop._state_run
  - Calls: hook methods, _run_loop
- `_run_loop(spec, messages, tools_used, total_usage, hook)`:
  - Process: for each iteration:
    1. **NEW: calls `ContextGovernor.prepare_for_model()` if governance_config set** — sanitizes messages
    2. hook.before_iteration
    3. calls provider.chat_stream_with_retry (streaming)
    4. hook.on_stream_end
    5. if tool_calls: execute tools, check injection → continue
    6. if text: check injection (extends turn if messages), else return result
  - Called by: run()
  - Calls: ContextGovernor.prepare_for_model (NEW), hook methods, provider.chat_stream_with_retry, spec.tools.execute, spec.injection_callback, _build_assistant_message, _accumulate_usage
- `_build_assistant_message(response)` (static): LLMResponse → assistant message dict
- `_accumulate_usage(total, response)` (static): accumulates token usage

---

## `loop.py`

### `enum TurnState`: RESTORE → COMPACT → BUILD → RUN → SAVE → RESPOND → DONE

### `class TurnContext`
- Holds: msg, session_key, state, session, summary, history, initial_messages, result, outbound

### `class StreamPublishingHook(AgentHook)`
- `on_stream(ctx, delta)`: publishes StreamDeltaEvent (finished=False)
- `on_stream_end(ctx)`: publishes StreamDeltaEvent (finished=True)

### `class AgentLoop`
- `__init__(...)`: stores all dependencies, init locks, pending queues, creates AgentRunner
- `run()`: loop consuming bus.inbound → creates task for _dispatch(msg)
- `stop()`: sets running = False
- `_get_or_create_queue(session_key)`: returns per-session asyncio.Queue (maxsize=20)
- `_dispatch(msg)`: per-session lock → if busy, queue; else process → publish → _drain_leftover
- `_drain_leftover(session_key)`: republishes queued messages back to bus.inbound
- `_process_message(msg, session_key)`: state machine calling _state_* handlers
- `_state_restore(ctx)`: SessionManager.get_or_create → "ok"
- `_state_compact(ctx)`: Consolidator.maybe_consolidate → "ok"
- `_state_build(ctx)`: get_history + build_messages → "ok"
- `_state_run(ctx)`:
  - Creates StreamPublishingHook + CompositeHook
  - Creates injection_callback draining _pending_queues[ctx.session_key]
  - Creates AgentRunSpec with injection_callback → AgentRunner.run
  - Called by: AgentRunner.run
- `_state_save(ctx)`: import_messages + save → "ok"
- `_state_respond(ctx)`: creates OutboundMessage → "ok"

---

## `main.py`

### `ainput(prompt)` (function)
- Async wrapper around input()

### `main()` (function)
- Input: command-line arg for session_key (default: "default")
- Process:
  1. Creates ToolRegistry, registers EchoTool
  2. Creates OpenAICompatProvider from env, Consolidator, SessionManager, ContextBuilder
  3. Calculates replay_budget
  4. Creates MessageBus and AgentLoop, starts loop task
  5. CLI loop: reads user input, handles /exit, /history, /new commands
  6. Publishes InboundMessage → consumes OutboundMessage → prints response
- Output: prints responses to console

---

## End-to-End Data Flow (with Context Governance)

```
User Input (CLI)
  │
  ▼
main.py → main()
  │  publishes InboundMessage → bus.inbound
  ▼
AgentLoop.run()
  │  consumes bus.inbound → _dispatch(msg)
  ▼
AgentLoop._dispatch(msg)
  │  per-session lock
  │  [if free] → _process_message() [state machine]
  │
  ├─ _state_restore  → SessionManager.get_or_create() → Session
  ├─ _state_compact  → Consolidator.maybe_consolidate() → summary
  ├─ _state_build    → ContextBuilder.build_messages() → initial_messages
  ├─ _state_run      → AgentRunner.run(AgentRunSpec)
  │                      │
  │                      ▼
  │                    AgentRunner._run_loop()
  │                      │
  │                      │  ContextGovernor.prepare_for_model() ← NEW
  │                      │    ├─ strip_placeholder_assistant_messages
  │                      │    ├─ strip_malformed_tool_calls
  │                      │    ├─ drop_orphan_tool_results
  │                      │    ├─ backfill_missing_tool_results
  │                      │    ├─ apply_tool_result_budget
  │                      │    ├─ compact_inflight_overflow
  │                      │    ├─ snip_history
  │                      │    └─ drop_orphan + backfill (repeat)
  │                      │
  │                      │  LLM call → LLMResponse
  │                      │  [if tool_calls]: execute tools → injection → continue
  │                      │  [if text]: injection check → return or extend turn
  │                      │
  │                      ▼
  │                    AgentRunResult
  │
  ├─ _state_save     → Session.import_messages() + SessionManager.save()
  └─ _state_respond  → OutboundMessage → bus.outbound
  │
  ▼
main.py → consumes bus.outbound → prints response
```

### Key Differences from Step 13 (Context Governance)

| Aspect | Step 13 | Step 14 |
|--------|---------|---------|
| Message preprocessing | None | `ContextGovernor.prepare_for_model()` before each iteration |
| Tool result cleanup | None | Strips malformed, drops orphans, backfills missing |
| Token budget management | Only via Consolidator (between turns) | Plus inflight compaction + history snipping (per iteration) |
| AgentRunSpec | No governance_config | Has `governance_config: ContextGovernanceConfig` |
| New files | — | `governance.py`, `helpers.py` |
| Runner._run_loop | Simple loop | Calls `_GOVERNOR.prepare_for_model(spec.governance_config, ...)` each iteration |
