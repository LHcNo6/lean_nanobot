## Step 12 — Data Flow Documentation

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
  - Called by: tests, ContextBuilder (indirectly)
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
- `AgentRunSpec`: initial_messages, tools, provider, max_iterations, model, temperature, max_tokens, hook, session_key
- `AgentRunResult`: final_content, messages, tools_used, usage, stop_reason; plus total_prompt_tokens / total_completion_tokens properties

### `class AgentRunner`
- `run(spec)`:
  - Input: AgentRunSpec
  - Process: calls hook.before_run → _run_loop → hook.after_run/on_error → hook.on_finally
  - Output: AgentRunResult
  - Called by: loop.py → AgentLoop._state_run
  - Calls: hook methods, _run_loop
- `_run_loop(spec, messages, tools_used, total_usage, hook)`:
  - Process: for each iteration:
    1. hook.before_iteration
    2. calls provider.chat_stream_with_retry (streaming with on_delta callback)
    3. hook.on_stream_end
    4. if tool_calls → builds assistant msg, executes tools, appends tool results, hook.after_iteration, continue
    5. if text response → appends assistant msg, hook.after_iteration, return AgentRunResult
  - Called by: run()
  - Calls: hook methods, provider.chat_stream_with_retry, spec.tools.execute, _build_assistant_message, _accumulate_usage
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
- `__init__(bus, provider, registry, session_manager, context_builder, consolidator, identity, replay_budget, hooks)`:
  - Stores all dependencies, init locks, creates AgentRunner
- `run()`:
  - Process: loop consuming bus.inbound → creates task for _dispatch(msg)
  - Called by: main.py → main()
  - Output: runs indefinitely until stop()
- `stop()`: sets running = False
- `_dispatch(msg)`:
  - Input: InboundMessage
  - Process: gets/per-session lock → _process_message → publish outbound
  - Called by: run()
  - Calls: _process_message, bus.publish_outbound
- `_process_message(msg, session_key)`:
  - Input: InboundMessage, session_key
  - Process: state machine loop (RESTORE → ... → DONE), catches errors
  - Output: OutboundMessage or None
  - Calls: _state_restore, _state_compact, _state_build, _state_run, _state_save, _state_respond
- `_state_restore(ctx)`:
  - Process: gets or creates Session
  - Output: "ok"
- `_state_compact(ctx)`:
  - Process: runs Consolidator.maybe_consolidate
  - Output: "ok"
- `_state_build(ctx)`:
  - Process: gets history → builds initial messages via ContextBuilder
  - Output: "ok"
- `_state_run(ctx)`:
  - Process: creates StreamPublishingHook, composite hook, AgentRunSpec → AgentRunner.run
  - Output: "ok"
  - Calls: AgentRunner.run
- `_state_save(ctx)`:
  - Process: imports new messages into session, saves via SessionManager
  - Output: "ok"
- `_state_respond(ctx)`:
  - Process: creates OutboundMessage with result content + metadata
  - Output: "ok"

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

## End-to-End Data Flow

```
User Input (CLI)
  │
  ▼
main.py → main()
  │  publishes InboundMessage → bus.inbound
  ▼
AgentLoop.run()
  │  consumes bus.inbound → _dispatch
  ▼
AgentLoop._dispatch()
  │  per-session lock → _process_message
  ▼
AgentLoop._process_message()  [state machine]
  │
  ├─ _state_restore  → SessionManager.get_or_create() → Session (from cache/JSONL)
  ├─ _state_compact  → Consolidator.maybe_consolidate() → summary (or None)
  │                      └─ calls LLMProvider.chat_with_retry() for summarization
  ├─ _state_build    → Session.get_history() + ContextBuilder.build_messages() → initial_messages
  ├─ _state_run      → AgentRunner.run(AgentRunSpec)
  │                      └─ AgentRunner._run_loop()
  │                          ├─ hook.before_iteration → hook.on_stream (per delta) → hook.on_stream_end
  │                          ├─ LLMProvider.chat_stream_with_retry() → LLMResponse
  │                          │    └─ OpenAICompatProvider.chat_stream() → OpenAI API
  │                          ├─ if tool_calls → ToolRegistry.execute() → ToolResult
  │                          │    └─ EchoTool.execute() → ToolResult("Echo: ...")
  │                          └─ returns AgentRunResult
  │                      └─ hook lifecycle: before_run → after_run/on_error → on_finally
  ├─ _state_save     → Session.import_messages() + SessionManager.save()
  └─ _state_respond  → creates OutboundMessage
  │
  ▼
AgentLoop._dispatch()
  │  publishes OutboundMessage → bus.outbound
  ▼
main.py → main()
  │  consumes bus.outbound → prints response
  ▼
Console Output
```

### Key Data Transformations

| From | To | Data |
|------|----|------|
| User input (string) | InboundMessage | content, chat_id=session_key |
| Session + messages | summary (string) | Consolidator.maybe_consolidate (token estimation → LLM summarization) |
| Bootstrap files + summary | system_prompt (string) | ContextBuilder.build_system_prompt (file reads + concatenation) |
| system_prompt + history + user msg | message list | ContextBuilder.build_messages |
| message list | LLMResponse | LLMProvider.chat_stream_with_retry → OpenAI API |
| LLMResponse (tool_calls) | assistant msg + tool results | AgentRunner._run_loop (tool execution, message appending) |
| AgentRunResult messages | Session messages | Session.import_messages (skipping history prefix) |
| AgentRunResult | OutboundMessage | AgentLoop._state_respond (content + metadata) |
