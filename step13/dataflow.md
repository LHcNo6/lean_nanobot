## Step 13 — Data Flow Documentation

**New in Step 13 vs Step 12:** Mid-turn injection. While the runner is processing a turn, if additional messages arrive for the same session (from the user while the LLM is still responding), they are queued and injected into the ongoing conversation rather than blocked.

---

## `llm.py`

### Dataclasses (no logic, pure data)
- `ToolCallRequest`: id, name, arguments
- `LLMResponse`: content, tool_calls, finish_reason, usage
- `RetryConfig`: max_retries, base_delay, max_delay, retry_mode

---

## `events.py`

### Dataclasses (no logic, pure data)
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

## `hook.py`

### Dataclasses
- `AgentHookContext`: iteration, messages, session_key, response, usage, tool_calls, tool_results, final_content, stop_reason, error, stream_content
- `AgentRunHookContext`: messages, final_content, tools_used, usage, stop_reason, error, exception

### `class AgentHook` (abstract base)
- `before_run(ctx)`: called before AgentRunner.run()
- `after_run(ctx)`: called after successful run
- `on_error(ctx)`: called on exception
- `on_finally(ctx)`: always called in finally block
- `before_iteration(ctx)`: called before each LLM iteration
- `after_iteration(ctx)`: called after each LLM iteration
- `on_stream(ctx, delta)`: called on each stream chunk
- `on_stream_end(ctx)`: called when stream finishes

### `class CompositeHook(AgentHook)`
- `__init__(hooks)`: stores list of AgentHook
- `_for_each(method, context)`: iterates hooks, catches exceptions
- Delegates all hook methods to `_for_each`
- `on_stream` / `on_stream_end`: specialized iteration with try/catch per hook
  - Called by: AgentRunner._run_loop → spec.provider.chat_stream_with_retry (on_delta)

---

## `context.py`

### `class ContextBuilder`
- `__init__(workspace, bootstrap_files)`: config
- `build_system_prompt(identity, session_summary)`:
  - Input: identity string or default, optional session_summary
  - Process: reads bootstrap files (AGENTS.md, SOUL.md, USER.md) from workspace if they exist
  - Output: combined system prompt string
  - Called by: build_messages
- `build_messages(current_message, history, identity, session_summary)`:
  - Input: current user message, optional history, identity, session_summary
  - Process: builds system prompt + history + current user message
  - Output: list of message dicts
  - Called by: loop.py → AgentLoop._state_build

---

## `consolidation.py`

### `estimate_message_tokens(msg)` (function)
- Input: single message dict
- Process: estimates token count by string length heuristic
- Output: int token count
- Called by: estimate_prompt_tokens, Consolidator._find_boundary, Session.get_history

### `estimate_prompt_tokens(messages)` (function)
- Input: list of message dicts
- Output: int total estimated tokens
- Called by: Consolidator.maybe_consolidate

### `class Consolidator`
- `__init__(provider, consolidation_ratio)`: stores LLM provider and ratio
- `maybe_consolidate(session, max_tokens, model)`:
  - Input: Session object, max_tokens budget, optional model
  - Process: if unconsolidated messages exceed target, finds boundary, optionally archives via LLM, updates session.last_consolidated
  - Output: summary string or None
  - Called by: loop.py → AgentLoop._state_compact
  - Calls: estimate_prompt_tokens, _find_boundary, _archive
- `_find_boundary(unconsolidated, target_tokens)` (static):
  - Input: messages list, target token budget
  - Process: finds cut point preserving most recent messages up to budget
  - Output: boundary index
- `_archive(messages, model)`:
  - Input: messages to archive, optional model
  - Process: calls provider.chat_with_retry to summarize
  - Output: summary string or None on failure
  - Calls: _format_messages
- `_format_messages(messages)` (static):
  - Input: messages list
  - Output: formatted string for summarization prompt

---

## `tool.py`

### `class ToolResult(str)`
- `__new__(content, is_error)`: creates string subclass with is_error flag
- `error(content)`: classmethod returning ToolResult with is_error=True

### `class Tool(ABC)`
- `name` / `description` / `parameters`: abstract properties
- `execute(**kwargs)`: async, raises NotImplementedError
- `to_schema()`: returns OpenAI-compatible tool schema dict
  - Called by: ToolRegistry.get_definitions

### `class ToolRegistry`
- `__init__()`: creates dict for tool lookup
- `register(tool)`: stores tool by name
- `unregister(name)`: removes tool
- `get(name)`: returns Tool or None
- `has(name)`: bool check
- `get_definitions()`: returns list of tool schemas
  - Called by: AgentRunner._run_loop, runner tests
- `execute(name, **params)`: async, executes tool by name, catches errors
  - Called by: AgentRunner._run_loop

---

## `tools/echo.py`

### `class EchoTool(Tool)`
- `name`: "echo"
- `description`: "Echoes back the input text."
- `parameters`: schema with "text" string property
- `execute(**kwargs)`: returns ToolResult("Echo: {text}")
  - Called by: ToolRegistry.execute

---

## `session.py`

### `safe_filename(name)` (function)
- Input: string
- Process: replaces unsafe filesystem characters
- Output: safe string

### `ensure_dir(path)` (function)
- Creates directory if not exists
- Returns Path

### `class Session`
- `__init__(key, ...)`: stores key, messages list, timestamps, metadata, last_consolidated index
- `add_message(role, content, **kwargs)`:
  - Input: role, content, extra fields
  - Process: creates message dict with timestamp, appends
  - Output: the message dict
- `import_messages(messages)`:
  - Input: list of message dicts
  - Process: adds timestamp if missing, appends all
  - Called by: loop.py → AgentLoop._state_save
- `get_history(max_messages, max_tokens)`:
  - Input: limits
  - Process: slices from last_consolidated, optionally trims by token count or message count
  - Output: list of message dicts
  - Called by: loop.py → AgentLoop._state_build

### `class SessionManager`
- `__init__(workspace)`: sets sessions directory, inits cache
- `_session_path(key)`: returns Path to JSONL file
- `get_or_create(key)`:
  - Input: session key
  - Process: checks cache → tries _load → creates new Session
  - Output: Session
  - Called by: loop.py → AgentLoop._state_restore, main.py → main()
- `_load(key)`:
  - Input: session key
  - Process: reads JSONL file, reconstructs Session
  - Output: Session or None
- `save(session, fsync)`:
  - Input: Session, optional fsync flag
  - Process: writes metadata + messages as JSONL to temp file → atomic replace
  - Called by: loop.py → AgentLoop._state_save

---

## `provider.py`

### `_is_retryable_exception(exc)` (function)
- Determines if an exception is retryable (timeout, connection, rate limit, 5xx)

### `_backoff_delay(attempt, config)` (function)
- Calculates exponential backoff with jitter

### `class _StreamGuard`
- Simple flag: delta_delivered

### `class LLMProvider(ABC)`
- `chat(messages, tools, model, temperature, max_tokens)` (abstract):
  - Input: message list, tool schemas, model params
  - Output: LLMResponse
- `chat_stream(messages, ...)`:
  - Default implementation: calls chat(), then calls on_content_delta with full content
  - Output: LLMResponse
- `chat_with_retry(messages, ...)`:
  - Input: same as chat + optional RetryConfig
  - Process: calls chat in loop with exponential backoff on retryable errors
  - Output: LLMResponse
  - Called by: Consolidator._archive
- `chat_stream_with_retry(messages, ...)`:
  - Input: same as chat_stream + optional RetryConfig
  - Process: calls chat_stream in loop, but only retries if no delta delivered
  - Output: LLMResponse
  - Called by: AgentRunner._run_loop

---

## `openai_compat_provider.py`

### `class OpenAICompatProvider(LLMProvider)`
- `__init__(api_key, api_base, model)`: creates AsyncOpenAI client
- `model`: returns default model name
- `chat(messages, tools, model, temperature, max_tokens)`:
  - Process: builds kwargs → calls OpenAI API → parses response
  - Output: LLMResponse
  - Calls: _build_kwargs, _parse_response
- `chat_stream(messages, tools, model, temperature, max_tokens, on_content_delta)`:
  - Process: builds kwargs with stream=True → iterates chunks → calls on_content_delta per chunk
  - Output: LLMResponse assembled from chunks
  - Calls: _build_kwargs, _assemble_from_chunks
- `_build_kwargs(...)`: returns dict for OpenAI API call
- `_parse_response(resp)`:
  - Input: OpenAI API response
  - Output: LLMResponse
  - Calls: _parse_tool_calls
- `_assemble_from_chunks(chunks)`:
  - Input: list of stream chunks
  - Process: concatenates content, assembles tool calls, extracts usage
  - Output: LLMResponse
- `_parse_tool_calls(tool_calls_raw)` (static):
  - Input: raw tool calls from API
  - Output: list of ToolCallRequest
- `from_env()` (classmethod):
  - Reads OPENAI_API_KEY, OPENAI_API_BASE, OPENAI_MODEL from env
  - Returns OpenAICompatProvider instance
  - Called by: main.py → main()

---

## `runner.py`

### Dataclasses
- `AgentRunSpec`: initial_messages, tools, provider, max_iterations, model, temperature, max_tokens, hook, session_key, **injection_callback** (NEW)
- `AgentRunResult`: final_content, messages, tools_used, usage, stop_reason; total_prompt_tokens / total_completion_tokens properties

### `class AgentRunner`
- `run(spec)`:
  - Input: AgentRunSpec
  - Process: calls hook.before_run → _run_loop → hook.after_run/on_error → hook.on_finally
  - Output: AgentRunResult
  - Called by: loop.py → AgentLoop._state_run
  - Calls: hook methods, _run_loop
- `_run_loop(spec, messages, tools_used, total_usage, hook)` — **modified with injection**:
  - Process: for each iteration:
    1. hook.before_iteration
    2. calls provider.chat_stream_with_retry (streaming with on_delta callback)
    3. hook.on_stream_end
    4. **if tool_calls**:
       - builds assistant msg, executes tools, appends tool results
       - hook.after_iteration
       - **NEW: calls spec.injection_callback() → appends injected messages**
       - continues loop
    5. **if text response**:
       - appends assistant msg, hook.after_iteration
       - **NEW: calls spec.injection_callback() → if messages returned, appends them and continues loop** (extends turn)
       - otherwise returns AgentRunResult
  - Called by: run()
  - Calls: hook methods, provider.chat_stream_with_retry, spec.tools.execute, spec.injection_callback (NEW), _build_assistant_message, _accumulate_usage
- `_build_assistant_message(response)` (static):
  - Input: LLMResponse
  - Output: assistant message dict with tool_calls
- `_accumulate_usage(total, response)` (static):
  - Input: running total dict, LLMResponse
  - Process: accumulates prompt_tokens and completion_tokens

---

## `loop.py`

### `enum TurnState`
- RESTORE → COMPACT → BUILD → RUN → SAVE → RESPOND → DONE

### `class TurnContext`
- Holds: msg, session_key, state, session, summary, history, initial_messages, result, outbound

### `class StreamPublishingHook(AgentHook)`
- `__init__(bus, chat_id, channel, session_key)`: stores bus reference
- `on_stream(ctx, delta)`:
  - If delta non-empty, publishes StreamDeltaEvent (finished=False) to bus.outbound
- `on_stream_end(ctx)`:
  - Publishes StreamDeltaEvent (finished=True) to bus.outbound

### `class AgentLoop`
- `__init__(...)`: stores all dependencies, init locks, init **pending queues** (NEW), creates AgentRunner
- `run()`:
  - Process: loop consuming bus.inbound → creates task for _dispatch(msg)
  - Called by: main.py → main()
- `stop()`: sets running = False
- `_get_or_create_queue(session_key)` (NEW):
  - Returns a per-session asyncio.Queue (maxsize=20), creating if needed
  - Called by: _dispatch, _state_run
- `_dispatch(msg)` — **modified**:
  - Input: InboundMessage
  - Process: gets per-session lock
    - **If lock is already held (session busy): queues the message in _pending_queues[session_key]**
    - If lock free: processes message → publishes outbound → **calls _drain_leftover** (NEW)
  - Calls: _process_message, bus.publish_outbound, _get_or_create_queue, _drain_leftover
- `_drain_leftover(session_key)` (NEW):
  - Input: session_key
  - Process: checks pending queue; if non-empty, republishes message to bus.inbound (to be picked up by the main loop)
  - Called by: _dispatch (after session lock released)
- `_process_message(msg, session_key)`:
  - Same state machine as step12
  - Calls: _state_restore, _state_compact, _state_build, _state_run, _state_save, _state_respond
- `_state_restore(ctx)`: gets/creates Session → "ok"
- `_state_compact(ctx)`: runs Consolidator → "ok"
- `_state_build(ctx)`: gets history + builds messages → "ok"
- `_state_run(ctx)` — **modified**:
  - Creates StreamPublishingHook + CompositeHook
  - **NEW: creates injection_callback closure** that drains `_pending_queues[ctx.session_key]`
  - Creates AgentRunSpec with injection_callback → calls AgentRunner.run
  - Output: "ok"
  - Calls: AgentRunner.run
- `_state_save(ctx)`: imports messages, saves session → "ok"
- `_state_respond(ctx)`: creates OutboundMessage → "ok"

---

## `main.py`

### `ainput(prompt)` (function)
- Async wrapper around input() using executor

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
- Calls: MessageBus, AgentLoop, SessionManager, OpenAICompatProvider, Consolidator, ContextBuilder, ToolRegistry, EchoTool

---

## End-to-End Data Flow (with Mid-Turn Injection)

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
  │  gets per-session lock
  │
  ├─ [if lock is FREE]:
  │     ▼
  │   _process_message()  [state machine]
  │     ├─ _state_restore  → SessionManager.get_or_create() → Session
  │     ├─ _state_compact  → Consolidator.maybe_consolidate() → summary
  │     ├─ _state_build    → ContextBuilder.build_messages() → initial_messages
  │     ├─ _state_run      → AgentRunner.run(AgentRunSpec)
  │     │                      │
  │     │                      │  AgentRunner._run_loop()
  │     │                      │    ├─ LLM call → LLMResponse
  │     │                      │    ├─ [if tool_calls]:
  │     │                      │    │    execute tools
  │     │                      │    │    └─ injection_callback() — drains pending msgs ← NEW
  │     │                      │    │       from _pending_queues[session_key]
  │     │                      │    │    └─ appends injected user messages → continue loop
  │     │                      │    ├─ [if text response]:
  │     │                      │    │    injection_callback() — drains pending msgs ← NEW
  │     │                      │    │    ├─ [if injected msgs]: append, continue loop (extends turn)
  │     │                      │    │    └─ [if empty]: return AgentRunResult
  │     │                      │    └─ returns AgentRunResult
  │     │                      ↓
  │     ├─ _state_save     → Session.import_messages() + SessionManager.save()
  │     └─ _state_respond  → OutboundMessage
  │     ▼
  │   publish OutboundMessage → bus.outbound
  │
  │  └─ _drain_leftover(session_key) ← NEW
  │       checks pending queue
  │       [if non-empty]: republishes to bus.inbound
  │       (these will be consumed on next loop iteration)
  │
  └─ [if lock is BUSY]:
        queued in _pending_queues[session_key] ← NEW
        (will be injected mid-turn or drained after lock release)

Output Flow:
  main.py → consumes bus.outbound → prints response to console
```

### Key Differences from Step 12 (Mid-Turn Injection)

| Aspect | Step 12 | Step 13 |
|--------|---------|---------|
| Concurrent messages | Blocked by per-session lock | Queued in `_pending_queues` |
| Runner._run_loop | No injection | Calls `injection_callback` after tool execution and before final response |
| Turn extension | One response per turn | Injected messages can extend the turn (continue loop) |
| AgentRunSpec | No injection_callback | Has `injection_callback: Callable[[], Awaitable[list[dict]]]` |
| AgentLoop._dispatch | Simple lock → process | Lock → if busy, queue; after process, `_drain_leftover` |
| AgentLoop | No pending queues | `_pending_queues: dict[str, asyncio.Queue]`, `_get_or_create_queue()`, `_drain_leftover()` |
| _state_run | No injection | Creates injection_callback closure tied to session's pending queue |
