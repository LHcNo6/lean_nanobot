# Step 8 Data Flow

## llm.py

*(Identical to step6/step7)*

### `@dataclass ToolCallRequest`
### `@dataclass LLMResponse`
### `@dataclass RetryConfig`

---

## context.py

*(Extended from step6/step7)*

### `class ContextBuilder`
- **`build_system_prompt(identity, session_summary)`**:
  - Input: optional identity string, optional session summary string
  - Process: builds identity + bootstrap files + `[Archived Context Summary]` section if summary provided
  - Output: combined string
  - Called by: `build_messages()`

- **`build_messages(current_message, history, identity, session_summary)`**:
  - Input: user message, optional history, identity, session summary
  - Process: calls `build_system_prompt(identity, session_summary)` → `[system, *history, user]`
  - Output: `list[dict]`
  - Called by: `main.py → main()`

---

## tool.py

*(Identical to step6/step7)*

### `class ToolResult`
### `class Tool(ABC)` / `class ToolRegistry`

---

## tools/echo.py

*(Identical to step6/step7)*

### `class EchoTool(Tool)`

---

## provider.py

*(Identical to step6/step7)*

### `LLMProvider(ABC)` — `chat()`, `chat_stream()`, `chat_with_retry()`, `chat_stream_with_retry()`

---

## openai_compat_provider.py

*(Same as step6/step7, plus `model` property)*

### `class OpenAICompatProvider(LLMProvider)`
- **`model`** (property): exposes `self._default_model`
  - Called by: `main.py → main()` → passed to `Consolidator.maybe_consolidate()`

---

## runner.py

*(Identical to step6/step7)*

### `@dataclass AgentRunSpec`
### `@dataclass AgentRunResult`
### `class AgentRunner` — tool-calling loop

---

## session.py

*(Same as step7, `get_history` extended with `max_tokens`)*

### `safe_filename(name)`, `ensure_dir(path)` — module-level helpers

### `@dataclass Session`
- **`add_message(role, content, **kwargs)`** — appends with timestamp
- **`import_messages(messages)`** — appends with timestamp, updates `updated_at`
- **`get_history(max_messages, max_tokens)`**:
  - Input: `max_messages=50`, `max_tokens=0` (0 = no token limit)
  - Process:
    1. Slices `self.messages[self.last_consolidated:]`
    2. If `max_tokens > 0`: iterates reversed, uses `estimate_message_tokens()` to keep messages under budget
    3. Then applies `max_messages` limit
  - Output: `list[dict]` — history subset for context
  - Called by: `main.py → main()`, `Consolidator` indirectly

### `class SessionManager`
- **`get_or_create(key)`** — cache → load → create new
- **`_load(key)`** — reads JSONL, parses metadata + messages
- **`save(session, *, fsync)`** — atomic write via tmp file + `os.replace()`

---

## consolidation.py

### `estimate_message_tokens(msg)` (module-level)
- Input: message dict
- Process: sums content, name, tool_call_id, tool_calls JSON; estimates `max(4, len(payload)//4 + 4)`
- Output: `int` token count
- Called by: `estimate_prompt_tokens()`, `Session.get_history()`, `Consolidator._find_boundary()`

### `estimate_prompt_tokens(messages)` (module-level)
- Input: message list
- Process: sums per-message tokens + 4-token overhead per message
- Output: `int`
- Called by: `Consolidator.maybe_consolidate()`

### `@dataclass Consolidator`
- Fields: `provider` (optional, for LLM-based summarization), `consolidation_ratio` (default 0.5)

- **`maybe_consolidate(session, max_tokens, model)`**:
  - Input: `Session`, budget `max_tokens`, optional model
  - Process:
    1. Gets `unconsolidated = session.messages[session.last_consolidated:]`
    2. If no unconsolidated messages → return `None`
    3. Estimates token count; if under `max_tokens * consolidation_ratio` → return `None`
    4. Calls `_find_boundary(unconsolidated, target)` to determine how many to archive
    5. If boundary <= 0 → return `None`
    6. If provider is set: calls `_archive(to_archive, model)` → summary string
    7. Updates `session.last_consolidated += boundary`
    8. Stores summary in `session.metadata["_last_summary"]`
  - Output: summary string or `None`
  - Called by: `main.py → main()` (before each turn)

- **`_find_boundary(unconsolidated, target_tokens)`** (static):
  - Input: message list, target token budget
  - Process: iterates from end backward, keeps messages under budget, then advances to next `user` role boundary
  - Output: integer count of messages to archive

- **`_archive(messages, model)`** (async):
  - Input: messages to summarize, optional model
  - Process: formats messages via `_format_messages()`, sends to `self.provider.chat_with_retry()` with archivist system prompt
  - Output: summary text or `None` on error

- **`_format_messages(messages)`** (static):
  - Input: message list
  - Process: formats as `[role]\ncontent[tool_calls: ...][tool_result for tool: ...]` with `---` separators
  - Output: formatted string

---

## main.py

### `ainput(prompt)` (async helper)

### `print_history(session)` — shows messages with `last_consolidated` marker

### `main()` (async)
- Input: `sys.argv[1]` → session_key (default: "default")
- Constants: `_DEMO_CONTEXT_WINDOW=1024`, `_SAFETY_BUFFER=128`, `_DEMO_MAX_TOKENS=128`
- Process (interactive REPL loop):
  1. Creates `ToolRegistry` with `EchoTool`
  2. Creates `OpenAICompatProvider.from_env()`
  3. Creates `Consolidator(provider=provider)`, `SessionManager(workspace=".")`, `ContextBuilder(workspace=".")`
  4. Computes `replay_budget = 1024 - 128 - 128 = 768`
  5. Loop:
     - Read user input
     - `/exit`, `/history`, `/new` commands
     - Normal message:
       1. `session_manager.get_or_create(key)` → `Session`
       2. `consolidator.maybe_consolidate(session, max_tokens=replay_budget, model=provider.model)` → summary or None
       3. `session.get_history(max_messages=50, max_tokens=replay_budget)` → token-budgeted history
       4. `context.build_messages(message, history=history, identity=..., session_summary=summary)` → messages (includes summary in system prompt)
       5. `AgentRunSpec(...)` → `AgentRunner().run(spec)` → `AgentRunResult`
       6. `session.import_messages(result.messages[skip:])`
       7. `session_manager.save(session)`

### End-to-End Flow:
```
User input loop
  → SessionManager.get_or_create(key) → Session
  → Consolidator.maybe_consolidate(session, max_tokens=replay_budget, model)
    → estimate_prompt_tokens(unconsolidated)
    → _find_boundary() — determines messages to archive
    → _archive() — LLM summarizes archived messages → summary string
    → updates session.last_consolidated, session.metadata["_last_summary"]
    → returns summary or None
  → Session.get_history(max_messages=50, max_tokens=replay_budget)
    → estimate_message_tokens() per message — trims from front under budget
    → returns history list
  → ContextBuilder.build_messages(message, history, identity, session_summary)
    → build_system_prompt() — includes Archived Context Summary section
    → [system + summary, *history, user] messages
  → AgentRunSpec → AgentRunner.run()
    → [loop] ToolRegistry → LLMProvider → Tool execution → AgentRunResult
  → Session.import_messages(result.messages[skip:]) → merge
  → SessionManager.save(session) → persist JSONL
  → [next iteration]
```
