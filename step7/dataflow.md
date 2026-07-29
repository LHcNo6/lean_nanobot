# Step 7 Data Flow

## llm.py

*(Identical to step6)*

### `@dataclass ToolCallRequest`
- Fields: `id: str`, `name: str`, `arguments: Any`

### `@dataclass LLMResponse`
- Fields: `content: str | None`, `tool_calls: list[ToolCallRequest]`, `finish_reason: str`, `usage: dict[str, int]`

### `@dataclass RetryConfig`
- Fields: `max_retries`, `base_delay`, `max_delay`, `retry_mode`

---

## context.py

*(Identical to step6)*

### `class ContextBuilder`
- **`build_system_prompt(identity)`**: reads bootstrap files from workspace, builds prompt string
- **`build_messages(current_message, history, identity)`**: assembles `[system, *history, user]` messages list
  - Called by: `main.py → main()`

---

## tool.py

*(Identical to step6)*

### `class ToolResult(str)`
### `class Tool(ABC)` — `name`, `description`, `parameters`, `execute()`, `to_schema()`
### `class ToolRegistry` — `register()`, `get()`, `get_definitions()`, `execute()`

---

## tools/echo.py

*(Identical to step6)*

### `class EchoTool(Tool)` — echoes back text

---

## provider.py

*(Identical to step6)*

### `LLMProvider(ABC)` — `chat()`, `chat_stream()`, `chat_with_retry()`, `chat_stream_with_retry()`

---

## openai_compat_provider.py

*(Identical to step6)*

### `class OpenAICompatProvider(LLMProvider)` — OpenAI API implementation with streaming, retries, tool call parsing, `from_env()` factory

---

## runner.py

*(Identical to step6)*

### `@dataclass AgentRunSpec` — spec with messages, tools, provider, limits
### `@dataclass AgentRunResult` — result container with token usage, messages, stop reason
### `class AgentRunner` — main loop: LLM call → tool execution → completion

---

## session.py

### `safe_filename(name)` (module-level)
- Input: raw session key string
- Process: replaces unsafe filesystem chars (`<>:"/\|?*`) with `_`
- Output: safe filename string
- Called by: `SessionManager._session_path()`

### `ensure_dir(path)` (module-level)
- Input: `Path`
- Process: creates directory if missing
- Output: the same `Path`

### `@dataclass Session`
- Fields: `key`, `messages: list[dict]`, `created_at`, `updated_at`, `metadata`, `last_consolidated`

- **`add_message(role, content, **kwargs)`**:
  - Input: role string, content string, extra kwargs (tool_calls, etc.)
  - Process: creates message dict with timestamp, appends to `self.messages`, updates `updated_at`
  - Output: the message dict
  - Called by: tests

- **`import_messages(messages)`**:
  - Input: list of message dicts (from `AgentRunResult.messages`)
  - Process: adds timestamp if missing, appends each to `self.messages`, updates `updated_at`
  - Called by: `main.py → main()` (post-run persistence)

- **`get_history(max_messages)`**:
  - Input: optional `max_messages` limit (default 50)
  - Process: slices `self.messages[self.last_consolidated:]`, applies max_messages from end
  - Output: `list[dict]` — history for next turn
  - Called by: `main.py → main()` (pre-run), tests

### `class SessionManager`
- **`__init__(workspace)`**: sets `sessions_dir = workspace/sessions/`, creates dir, initializes empty cache

- **`_session_path(key)`**: returns `Path(sessions_dir / safe_filename(key) + ".jsonl")`

- **`get_or_create(key)`**:
  - Input: session key string
  - Process: checks cache; if miss, calls `_load(key)` or creates new `Session(key=key)`
  - Output: `Session`
  - Called by: `main.py → main()`

- **`_load(key)`**:
  - Input: session key
  - Process: opens `.jsonl` file, reads first line as metadata (with `_type: "metadata"`), rest as messages. Catches JSON/OS errors.
  - Output: `Session` or `None` if file missing/corrupt

- **`save(session, *, fsync)`**:
  - Input: `Session`, optional fsync flag
  - Process: writes to `.jsonl.tmp`, writes metadata line then message lines, uses `os.replace()` for atomic swap. Cleans tmp on error.
  - Output: `None` (updates cache)
  - Called by: `main.py → main()` (after import_messages)

---

## main.py

### `ainput(prompt)` (async helper)
- Input: prompt string
- Process: runs `input()` in executor thread (non-blocking for asyncio)

### `print_history(session)`
- Input: `Session`
- Process: prints each message's role, content (first 80 chars), name

### `main()` (async)
- Input: `sys.argv[1]` → session_key (default: "default"), `sys.argv[2]` → identity
- Process (interactive REPL loop):
  1. Creates `ToolRegistry`, registers `EchoTool`
  2. Creates `OpenAICompatProvider.from_env()`
  3. Creates `SessionManager(workspace=".")` and `ContextBuilder(workspace=".")`
  4. Loop:
     - Read user input via `ainput()`
     - `/exit` → break
     - `/history` → `session_manager.get_or_create(key)`, `print_history()`, continue
     - `/new` → delete session file and cache, continue
     - Normal message:
       1. `session_manager.get_or_create(key)` → `Session`
       2. `session.get_history(max_messages=20)` → history list
       3. `context.build_messages(message, history=history, identity=...)` → messages
       4. `AgentRunSpec(messages, registry, provider, max_iterations=5)`
       5. `AgentRunner().run(spec)` → `AgentRunResult`
       6. Prints result (stop_reason, content, tokens, tools)
       7. `skip = 1 + len(history)`
       8. `session.import_messages(result.messages[skip:])` — appends new user+assistant+tool messages (excluding system prompt and replayed history)
       9. `session_manager.save(session)` — persists to disk

### End-to-End Flow:
```
User input loop
  → SessionManager.get_or_create(key) → Session (loaded from disk or new)
  → Session.get_history(max_messages=20) → history list
  → ContextBuilder.build_messages(message, history, identity) → messages
  → AgentRunSpec
  → AgentRunner.run()
    → [loop] ToolRegistry.get_definitions()
    → LLMProvider.chat_with_retry()
      → OpenAICompatProvider.chat() → OpenAI API → LLMResponse
    → [if tool_calls] ToolRegistry.execute() → EchoTool → ToolResult
    → AgentRunResult
  → Session.import_messages(result.messages[skip:]) → merges into Session
  → SessionManager.save(session) → writes JSONL to disk
  → [next iteration]
```
