# Data Flow — Step 9: MessageBus + Events

## End-to-End Flow

```
User CLI input
  → main() publishes InboundMessage to MessageBus.inbound
    → _agent_loop() consumes InboundMessage
      → Consolidator.maybe_consolidate() prunes/archives old session messages
      → Session.get_history() returns recent unconsolidated messages
      → ContextBuilder.build_messages() creates [system + history + user] prompt
      → AgentRunner.run() executes the agent loop:
          → Provider.chat_with_retry() → OpenAI API
          → ToolRegistry.execute() for each tool call
          → returns AgentRunResult
      → Session.import_messages() saves new messages
      → SessionManager.save() persists to JSONL
    → _agent_loop() publishes OutboundMessage to MessageBus.outbound
  → main() consumes OutboundMessage → prints to console
```

---

## bus.py

### `class MessageBus`
- `__init__()`: creates two `asyncio.Queue` — `inbound` and `outbound`
- `publish_inbound(msg)`: puts `InboundMessage` into `self.inbound` queue
- `consume_inbound()`: gets `InboundMessage` from `self.inbound` queue
  - Called by: `_agent_loop()` (main.py:51)
- `publish_outbound(msg)`: puts `OutboundMessage` into `self.outbound` queue
  - Called by: `_agent_loop()` (main.py:54,62,76,107)
- `consume_outbound()`: gets `OutboundMessage` from `self.outbound` queue
  - Called by: `main()` (main.py:148,155,161,166)
- `inbound_size` / `outbound_size`: queue size properties

---

## events.py

### `@dataclass InboundMessage`
- Fields: `content`, `channel`, `sender_id`, `chat_id`, `timestamp`, `session_key`, `metadata`
- Created in: `main()` → published to `MessageBus.inbound`

### `@dataclass OutboundMessage`
- Fields: `content`, `channel`, `chat_id`, `metadata`
- Created in: `_agent_loop()` → published to `MessageBus.outbound`

---

## llm.py

### `@dataclass ToolCallRequest`
- Fields: `id`, `name`, `arguments`
- Created by: `OpenAICompatProvider._parse_tool_calls()` and `_assemble_from_chunks()`
- Consumed by: `AgentRunner.run()` → `ToolRegistry.execute()`

### `@dataclass LLMResponse`
- Fields: `content`, `tool_calls`, `finish_reason`, `usage`
- Created by: `OpenAICompatProvider.chat()`, `chat_stream()`, `_assemble_from_chunks()`, `_parse_response()`
- Consumed by: `AgentRunner.run()`

### `@dataclass RetryConfig`
- Fields: `max_retries`, `base_delay`, `max_delay`, `retry_mode`
- Used by: `LLMProvider.chat_with_retry()`, `chat_stream_with_retry()`

---

## provider.py

### `estimate_message_tokens(msg)` / `estimate_prompt_tokens(messages)` (from consolidation.py, imported locally)
- Input: single message dict or list of message dicts
- Process: estimates token count based on string length
- Output: integer token estimate
- Called by: `Session.get_history()`, `Consolidator.maybe_consolidate()`, `Consolidator._find_boundary()`

### `_is_retryable_exception(exc)` → bool
- Checks if exception is retryable (timeout, rate limit, server error)

### `_backoff_delay(attempt, config)` → float
- Exponential backoff with jitter

### `class _StreamGuard`
- Tracks whether any content delta was delivered in a stream

### `class LLMProvider(ABC)`
- `chat()` — abstract, implemented by `OpenAICompatProvider`
- `chat_stream()` — default impl calls `chat()` then invokes `on_content_delta` callback
  - Called by: `chat_stream_with_retry()`
- `chat_with_retry()` — wraps `chat()` with retry logic
  - Called by: `AgentRunner.run()` (runner.py:51), `Consolidator._archive()` (consolidation.py:105)
- `chat_stream_with_retry()` — wraps `chat_stream()` with retry logic

---

## openai_compat_provider.py

### `class OpenAICompatProvider(LLMProvider)`
- `__init__(api_key, api_base, model)`: creates `AsyncOpenAI` client
- `model` property: returns `self._default_model`
- `chat(messages, tools, model, temperature, max_tokens)`:
  - Input: message list, optional tool schemas
  - Process: calls `_build_kwargs()` → `AsyncOpenAI.chat.completions.create()` → `_parse_response()`
  - Output: `LLMResponse`
  - Called by: `LLMProvider.chat_with_retry()`
- `chat_stream(messages, tools, model, temperature, max_tokens, on_content_delta)`:
  - Input: same as chat + optional streaming delta callback
  - Process: streams chunks from API → `_assemble_from_chunks()`
  - Output: `LLMResponse`
  - Called by: `LLMProvider.chat_stream_with_retry()`
- `_build_kwargs(...)` → dict: assembles API request kwargs
- `_parse_response(resp)` → `LLMResponse`: parses non-streaming API response
- `_assemble_from_chunks(chunks)` → `LLMResponse`: reassembles streaming chunks into single response
- `_parse_tool_calls(tool_calls_raw)` → `list[ToolCallRequest]`: extracts tool calls from API response
- `from_env()`: classmethod factory reading `OPENAI_API_KEY`, `OPENAI_API_BASE`, `OPENAI_MODEL` from env

---

## tool.py

### `class ToolResult(str)`
- `is_error` attribute, `.error()` classmethod for error results

### `class Tool(ABC)`
- Abstract properties: `name`, `description`, `parameters`
- `execute(**kwargs)` → `ToolResult` (overridden by subclasses)
- `to_schema()` → dict: OpenAI tool definition format

### `class ToolRegistry`
- `register(tool)`: stores tool by name
- `unregister(name)`: removes tool
- `get(name)` / `has(name)`: lookup
- `get_definitions()` → `list[dict]`: returns all tool schemas
  - Called by: `AgentRunner.run()` (runner.py:50)
- `execute(name, **params)` → `ToolResult`: looks up tool and calls its `execute()`
  - Called by: `AgentRunner.run()` (runner.py:65)

---

## tools/echo.py

### `class EchoTool(Tool)`
- `name`: "echo"
- `execute(**kwargs)` → `ToolResult("Echo: {text}")`
- Registered in: `main()` → `ToolRegistry.register()`

---

## runner.py

### `@dataclass AgentRunSpec`
- Fields: `initial_messages`, `tools` (ToolRegistry), `provider` (LLMProvider), `max_iterations`, `model`, `temperature`, `max_tokens`
- Created by: `_agent_loop()` (main.py:86-96)

### `@dataclass AgentRunResult`
- Fields: `final_content`, `messages`, `tools_used`, `usage`, `stop_reason`
- Properties: `total_prompt_tokens`, `total_completion_tokens`
- Created by: `AgentRunner.run()`

### `class AgentRunner`
- `run(spec)`:
  - Input: `AgentRunSpec`
  - Process:
    1. Copies `initial_messages`
    2. Loop (up to `max_iterations`):
       - Calls `spec.provider.chat_with_retry(messages, tools=...)`
       - If response has `tool_calls` + `finish_reason == "tool_calls"`:
         - Appends assistant message with tool_calls
         - For each tool call: `spec.tools.execute(name, **args)` → appends tool result message
         - Continues loop
       - Otherwise: appends final assistant message, returns `AgentRunResult`
    3. If max iterations reached: returns fallback result
  - Output: `AgentRunResult`
  - Called by: `_agent_loop()` (main.py:97)
- `_build_assistant_message(response)` → dict: converts `LLMResponse` to OpenAI-format assistant message with tool_calls
- `_accumulate_usage(total, response)`: sums prompt/completion tokens across iterations

---

## session.py

### `safe_filename(name)` → str: sanitizes filename
### `ensure_dir(path)` → Path: creates directory if missing

### `@dataclass Session`
- Fields: `key`, `messages`, `created_at`, `updated_at`, `metadata`, `last_consolidated`
- `add_message(role, content, **kwargs)` → dict: appends message to `self.messages`
- `import_messages(messages)`: appends list of messages with timestamps
  - Called by: `_agent_loop()` (main.py:100)
- `get_history(max_messages, max_tokens)` → list:
  - Input: max count + max token budget
  - Process: starts from `last_consolidated`, keeps recent messages within budget
  - Output: truncated message list
  - Called by: `_agent_loop()` (main.py:85)
  - Uses: `estimate_message_tokens` from consolidation.py

### `class SessionManager`
- `__init__(workspace)`: sets `sessions/` directory
- `_session_path(key)` → Path: `sessions/{key}.jsonl`
- `get_or_create(key)` → Session: returns cached session or loads/creates new one
  - Called by: `_agent_loop()` (main.py:66,79)
- `_load(key)` → Session | None: reads JSONL file, reconstructs Session
- `save(session, *, fsync)`: writes session to JSONL (atomic via tmp + replace)
  - Called by: `_agent_loop()` (main.py:101)

---

## consolidation.py

### `estimate_message_tokens(msg)` → int
- Input: single message dict
- Process: concatenates content, name, tool_call_id, tool_calls → length // 4 + 4
- Output: estimated token count

### `estimate_prompt_tokens(messages)` → int
- Sum of `estimate_message_tokens` per message + 4 × len(messages)

### `@dataclass Consolidator`
- Fields: `provider`, `consolidation_ratio` (default 0.5)
- `maybe_consolidate(session, max_tokens, model)` → str | None:
  - Input: Session, token budget, optional model
  - Process: estimates tokens of unconsolidated segment; if over target, finds boundary and optionally archives via LLM
  - Output: summary string or None
  - Called by: `_agent_loop()` (main.py:81-83)
- `_find_boundary(unconsolidated, target_tokens)` → int:
  - Reverse-scans messages to find split point within target tokens, ensures boundary lands on user message
- `_archive(messages, model)` → str | None:
  - Formats messages, calls `provider.chat_with_retry()` with archivist system prompt, returns summary
- `_format_messages(messages)` → str:
  - Converts message list to textual representation for summarization

---

## context.py

### `class ContextBuilder`
- `build_system_prompt(identity, session_summary)` → str:
  - Combines identity, bootstrap file content (AGENTS.md, SOUL.md, USER.md), and optional session summary
- `build_messages(current_message, history, identity, session_summary)` → list[dict]:
  - Calls `build_system_prompt()` → creates `[system, *history, user]` message list
  - Called by: `_agent_loop()` (main.py:87-92)

---

## main.py

### `ainput(prompt)` → str: async wrapper around `input()`

### `print_history(session)`: prints session messages (debug helper)

### `_agent_loop(bus, session_key, provider, registry, session_manager, context_builder, consolidator, identity, replay_budget)`
- Input: consumes `InboundMessage` from bus
- Process loop:
  1. Handle special commands (`exit`, `new`, `history`)
  2. Get/create session, run consolidation, get history
  3. Build messages via `ContextBuilder`, create `AgentRunSpec`
  4. Run `AgentRunner.run()` → get `AgentRunResult`
  5. Import new messages to session, save
  6. Publish `OutboundMessage` to bus
- Output: publishes to `MessageBus.outbound`

### `main()`
- Input: command-line arg (session key) or "default"
- Process: wires up all components (ToolRegistry, Provider, Consolidator, SessionManager, ContextBuilder, MessageBus), launches `_agent_loop` as task, then CLI loop reads input and publishes to bus
- Output: prints responses to console
