# Data Flow — Step 10: AgentLoop State Machine

## End-to-End Flow

```
User CLI input
  → main() publishes InboundMessage to MessageBus.inbound
    → AgentLoop.run() consumes InboundMessage
      → AgentLoop._dispatch() → per-session lock → _process_message()
        → TurnState machine (RESTORE → COMPACT → BUILD → RUN → SAVE → RESPOND → DONE):
          _state_restore:
            → SessionManager.get_or_create() → Session
          _state_compact:
            → Consolidator.maybe_consolidate() → summary
          _state_build:
            → Session.get_history() → history
            → ContextBuilder.build_messages() → initial_messages
          _state_run:
            → AgentRunner.run(spec) → AgentRunResult
              → Provider.chat_with_retry() → LLMResponse
              → ToolRegistry.execute() for tool calls
          _state_save:
            → Session.import_messages() + SessionManager.save()
          _state_respond:
            → builds OutboundMessage from AgentRunResult
      → AgentLoop._dispatch() publishes OutboundMessage to MessageBus.outbound
  → main() consumes OutboundMessage → prints to console
```

---

## bus.py

Same as step9: `MessageBus` with `inbound`/`outbound` `asyncio.Queue`.

---

## events.py

Same as step9: `InboundMessage`, `OutboundMessage` dataclasses.

---

## llm.py

Same as step9: `ToolCallRequest`, `LLMResponse`, `RetryConfig`.

---

## provider.py

Same as step9: `LLMProvider` ABC with `chat()`, `chat_stream()`, `chat_with_retry()`, `chat_stream_with_retry()`.

---

## openai_compat_provider.py

Same as step9: `OpenAICompatProvider(LLMProvider)` with `chat()`, `chat_stream()`, `from_env()`.

---

## tool.py

Same as step9: `ToolResult`, `Tool` ABC, `ToolRegistry`.

---

## tools/echo.py

Same as step9: `EchoTool` registered by `main()`.

---

## runner.py

Same as step9: `AgentRunSpec`, `AgentRunResult`, `AgentRunner`.

---

## session.py

Same as step9: `Session`, `SessionManager` with JSONL persistence.

---

## consolidation.py

Same as step9: `estimate_message_tokens()`, `estimate_prompt_tokens()`, `Consolidator`.

---

## context.py

Same as step9: `ContextBuilder.build_system_prompt()`, `build_messages()`.

---

## loop.py (NEW — replaces inline `_agent_loop` from step9)

### `class TurnState(Enum)`
- States: `RESTORE`, `COMPACT`, `BUILD`, `RUN`, `SAVE`, `RESPOND`, `DONE`
- Transition table: `{(RESTORE,"ok"): COMPACT, (COMPACT,"ok"): BUILD, (BUILD,"ok"): RUN, (RUN,"ok"): SAVE, (SAVE,"ok"): RESPOND, (RESPOND,"ok"): DONE}`

### `@dataclass TurnContext`
- Fields: `msg` (InboundMessage), `session_key`, `state` (TurnState), `session`, `summary`, `history`, `initial_messages`, `result` (AgentRunResult), `outbound` (OutboundMessage)
- Acts as shared mutable state flowing through state machine

### `class AgentLoop`
- `__init__(bus, provider, registry, session_manager, context_builder, consolidator, identity, replay_budget)`:
  - Stores all dependencies, creates `AgentRunner` instance and per-session locks dict
- `run()`:
  - Input: consumes `InboundMessage` from `bus.inbound`
  - Process: for each message, creates async task via `_dispatch()`
  - Output: none (responses published by `_dispatch`)
  - Called by: `main()` as background task
- `stop()`: sets `self.running = False`
- `_dispatch(msg)`:
  - Input: `InboundMessage`
  - Process: acquires per-session `asyncio.Lock`, calls `_process_message()`, publishes `OutboundMessage` on success
  - Called by: `run()` per message
- `_process_message(msg, session_key)` → `OutboundMessage | None`:
  - Input: message + session key
  - Process: creates `TurnContext`, runs state machine loop calling `_state_*` handlers, handles errors
  - Output: `OutboundMessage` on completion or error
  - Called by: `_dispatch()`
- State handlers (each returns event string `"ok"`):

  - `_state_restore(ctx)`:
    - Input: `ctx` with `session_key`
    - Process: calls `self.sessions.get_or_create(ctx.session_key)` → `ctx.session`
    - Called by: `_process_message()` when state == `RESTORE`

  - `_state_compact(ctx)`:
    - Input: `ctx` with `session`
    - Process: calls `self.consolidator.maybe_consolidate(session, ...)` → `ctx.summary`
    - Called by: `_process_message()` when state == `COMPACT`

  - `_state_build(ctx)`:
    - Input: `ctx` with `session`, `summary`
    - Process: calls `session.get_history()` → `ctx.history`, then `self.context.build_messages(...)` → `ctx.initial_messages`
    - Called by: `_process_message()` when state == `BUILD`

  - `_state_run(ctx)`:
    - Input: `ctx` with `initial_messages`
    - Process: builds `AgentRunSpec`, calls `self._runner.run(spec)` → `ctx.result`
    - Called by: `_process_message()` when state == `RUN`

  - `_state_save(ctx)`:
    - Input: `ctx` with `result`, `history`, `session`
    - Process: calls `session.import_messages(result.messages[skip:])` then `self.sessions.save(session)`
    - Called by: `_process_message()` when state == `SAVE`

  - `_state_respond(ctx)`:
    - Input: `ctx` with `result`
    - Process: creates `OutboundMessage` from `result.final_content` and metadata → `ctx.outbound`
    - Called by: `_process_message()` when state == `RESPOND`

---

## main.py

### `ainput(prompt)` → str: async wrapper around `input()`

### `main()`
- Input: command-line arg (session key) or "default"
- Process:
  1. Wires all components (ToolRegistry, Provider, Consolidator, SessionManager, ContextBuilder, MessageBus)
  2. Creates `AgentLoop` and runs it as background task
  3. CLI loop: reads user input, publishes `InboundMessage` to bus, waits for `OutboundMessage`, prints response
  4. Commands: `/exit` → `loop.stop()`, `/history` → reads session directly, `/new` → deletes session file
- Output: prints responses to console
