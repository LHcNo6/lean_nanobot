# Step 6 Data Flow

## llm.py

### `@dataclass ToolCallRequest`
- Fields: `id: str`, `name: str`, `arguments: Any`

### `@dataclass LLMResponse`
- Fields: `content: str | None`, `tool_calls: list[ToolCallRequest]`, `finish_reason: str`, `usage: dict[str, int]`
- Output container returned by providers; consumed by `AgentRunner.run()`.

### `@dataclass RetryConfig`
- Fields: `max_retries`, `base_delay`, `max_delay`, `retry_mode`

---

## context.py

### `class ContextBuilder`
- **`build_system_prompt(identity)`:**
  - Input: optional identity string
  - Process: prepends identity (or default), appends content from bootstrap files (AGENTS.md, SOUL.md, USER.md) if they exist on disk
  - Output: combined string with `---` separators
  - Called by: `build_messages()`

- **`build_messages(current_message, history, identity)`:**
  - Input: user message string, optional history list, optional identity
  - Process: calls `build_system_prompt()`, then assembles `[system, *history, user]` messages list
  - Output: `list[dict]` — ready for LLM API
  - Called by: `main.py → main()`

---

## tool.py

### `class ToolResult(str)`
- Subclass of `str` with `is_error: bool`
- `error(content)`: class method to create error result

### `class Tool(ABC)`
- Abstract properties: `name`, `description`, `parameters` (JSON schema)
- **`execute(**kwargs)`**: abstract async method returning `ToolResult`
- **`to_schema()`**: returns OpenAI-compatible tool definition dict

### `class ToolRegistry`
- **`register(tool)`**: stores tool by name
- **`get(name)`**: retrieves tool by name
- **`get_definitions()`**: returns list of all tool schemas (via `to_schema()`)
  - Output → passed to `LLMProvider.chat_with_retry()` as `tools` kwarg
- **`execute(name, **params)`**: looks up tool, calls `tool.execute()`, returns `ToolResult`
  - Called by: `AgentRunner.run()` for each tool call request

---

## tools/echo.py

### `class EchoTool(Tool)`
- Name: `"echo"`, expects `{"text": "string"}`
- **`execute(text)`**: returns `ToolResult("Echo: {text}")`

---

## provider.py

### `_is_retryable_exception(exc)` (module-level)
- Input: exception
- Process: checks for `TimeoutError`, OpenAI connection/timeout/rate-limit/5xx errors
- Output: `bool`

### `_backoff_delay(attempt, config)` (module-level)
- Input: attempt number, RetryConfig
- Process: exponential backoff with jitter
- Output: delay in seconds

### `class LLMProvider(ABC)`
- **`chat(messages, tools, model, temperature, max_tokens)`** (abstract):
  - Input: message list, optional tool definitions
  - Output: `LLMResponse`

- **`chat_stream(messages, tools, model, temperature, max_tokens, on_content_delta)`:**
  - Calls `self.chat()` by default, forwards content to callback
  - Output: `LLMResponse`

- **`chat_with_retry(messages, tools, model, temperature, max_tokens, retry_config)`:**
  - Wraps `chat()` with retry loop using `_is_retryable_exception()` and `_backoff_delay()`
  - Output: `LLMResponse`
  - Called by: `AgentRunner.run()`

- **`chat_stream_with_retry(messages, ..., on_content_delta, retry_config)`:**
  - Wraps `chat_stream()` with retry logic; does NOT retry if content delta was already delivered
  - Output: `LLMResponse`

---

## openai_compat_provider.py

### `class OpenAICompatProvider(LLMProvider)`
- **`__init__(api_key, api_base, model)`**: creates `AsyncOpenAI` client
- **`from_env()`** (classmethod): reads `OPENAI_API_KEY`, `OPENAI_API_BASE`, `OPENAI_MODEL` from env

- **`chat(messages, tools, model, temperature, max_tokens)`:**
  - Input: messages + optional tool definitions
  - Process: calls `_build_kwargs()`, sends to OpenAI API, calls `_parse_response()`
  - Output: `LLMResponse`
  - Called by: `LLMProvider.chat_with_retry()`

- **`chat_stream(messages, tools, model, temperature, max_tokens, on_content_delta)`:**
  - Process: streams chunks with `asyncio.wait_for` (30s idle timeout), calls `_assemble_from_chunks()`
  - Output: `LLMResponse`

- **`_build_kwargs(messages, tools, model, temperature, max_tokens)`:**
  - Output: dict for OpenAI API call

- **`_parse_response(resp)`:**
  - Input: raw OpenAI response object
  - Process: extracts choice, parses tool calls via `_parse_tool_calls()`, extracts usage
  - Output: `LLMResponse`

- **`_assemble_from_chunks(chunks)`:**
  - Input: list of stream chunks
  - Process: concatenates content delta, assembles tool calls by index, extracts usage from last chunk
  - Output: `LLMResponse`

- **`_parse_tool_calls(tool_calls_raw)`** (static):
  - Input: raw OpenAI tool call list
  - Process: parses JSON arguments, wraps in `ToolCallRequest`
  - Output: `list[ToolCallRequest]`

---

## runner.py

### `@dataclass AgentRunSpec`
- Fields: `initial_messages`, `tools: ToolRegistry`, `provider: LLMProvider`, `max_iterations`, `model`, `temperature`, `max_tokens`

### `@dataclass AgentRunResult`
- Fields: `final_content`, `messages`, `tools_used`, `usage`, `stop_reason`
- Properties: `total_prompt_tokens`, `total_completion_tokens`

### `class AgentRunner`
- **`run(spec)`**:
  - Input: `AgentRunSpec`
  - Process:
    1. Copies `initial_messages`
    2. Loops up to `max_iterations`:
       - Gets tool definitions from `spec.tools.get_definitions()`
       - Calls `spec.provider.chat_with_retry(messages, tools=tools_defs, ...)`
       - If `finish_reason == "tool_calls"`:
         - Calls `_build_assistant_message()` → appends to messages
         - For each `ToolCallRequest`: calls `spec.tools.execute(tc.name, **tc.arguments)` → appends tool result to messages
         - Continues loop
       - Else: appends assistant response → returns `AgentRunResult`
    3. If loop exhausts: returns `AgentRunResult` with `stop_reason="max_iterations"`
  - Output: `AgentRunResult`
  - Calls: `ToolRegistry.get_definitions()`, `LLMProvider.chat_with_retry()`, `ToolRegistry.execute()`

- **`_build_assistant_message(response)`** (static):
  - Input: `LLMResponse`
  - Process: converts `ToolCallRequest`s to OpenAI message format with `tool_calls` array
  - Output: assistant message dict

- **`_accumulate_usage(total, response)`** (static):
  - Input: running total dict, `LLMResponse`
  - Process: adds `prompt_tokens` and `completion_tokens`

---

## main.py

### `main()` (async)
- Input: command-line args (first arg → user message, default: "Say 'hello' using the echo tool")
- Process:
  1. Creates `ToolRegistry`, registers `EchoTool`
  2. Creates `OpenAICompatProvider.from_env()`
  3. Creates `ContextBuilder(workspace=".")`, calls `build_messages(message, identity=...)`
  4. Creates `AgentRunSpec` with messages, tools, provider, `max_iterations=5`
  5. Calls `AgentRunner().run(spec)` → gets `AgentRunResult`
- Output: prints stop_reason, final_content, token usage, tools_used

### End-to-End Flow:
```
CLI args
  → main()
    → ContextBuilder.build_messages() → [system, user] messages
    → AgentRunSpec
    → AgentRunner.run()
      → [loop] ToolRegistry.get_definitions()
      → LLMProvider.chat_with_retry()
        → OpenAICompatProvider.chat()
          → OpenAI API → LLMResponse
      → [if tool_calls] ToolRegistry.execute()
        → EchoTool.execute() → ToolResult
      → [loop continue or break]
    → AgentRunResult
  → stdout
```
