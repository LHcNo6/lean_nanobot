# Step 5 — Data Flow Documentation

## llm.py

### `@dataclass ToolCallRequest`
- Fields: `id: str`, `name: str`, `arguments: Any`
- Represents a single tool call requested by the LLM.

### `@dataclass LLMResponse`
- Fields: `content`, `tool_calls: list[ToolCallRequest]`, `finish_reason: str`, `usage: dict`
- Universal response type; `content` is `None` when `tool_calls` is populated.

### `@dataclass RetryConfig`
- Fields: `max_retries: int = 3`, `base_delay: float = 1.0`, `max_delay: float = 60.0`, `retry_mode: str = "standard"`

---

## provider.py

Identical to `step3/provider.py`.

### `_is_retryable_exception(exc)` → `bool`
- Classifies exceptions for retry logic.

### `_backoff_delay(attempt, config)` → `float`
- Computes exponential backoff with jitter.

### `class LLMProvider` (ABC)

| Method | Input | Process | Output | Called by |
|---|---|---|---|---|
| `chat()` | messages, tools, model, temp, max_tokens | abstract | `LLMResponse` | `chat_with_retry()`, `chat_stream()` |
| `chat_stream()` | same + `on_content_delta` | calls `chat()`, invokes callback | `LLMResponse` | `chat_stream_with_retry()` |
| `chat_with_retry()` | same + `RetryConfig` | retry loop around `chat()` | `LLMResponse` | `AgentRunner.run()` |
| `chat_stream_with_retry()` | same + `on_content_delta` + `RetryConfig` | retry loop around `chat_stream()` with `_StreamGuard` | `LLMResponse` | — |

---

## openai_compat_provider.py

Identical to `step3/openai_compat_provider.py` (imports from `step5.llm`, `step5.provider`).

### `class OpenAICompatProvider(LLMProvider)`

| Method | Input | Process | Output | Called by |
|---|---|---|---|---|
| `__init__()` | api_key, api_base, model | creates `AsyncOpenAI` client | — | `from_env()`, tests |
| `chat()` | standard params | `_build_kwargs()` → SDK → `_parse_response()` | `LLMResponse` | inherited `chat_with_retry()` |
| `chat_stream()` | standard + `on_content_delta` | `_build_kwargs(stream=True)` → iterate with timeout → `_assemble_from_chunks()` | `LLMResponse` | inherited `chat_stream_with_retry()` |
| `_build_kwargs()` | raw params | assembles API kwargs dict | `dict` | `chat()`, `chat_stream()` |
| `_parse_response()` | SDK response | extracts content, tool_calls, finish_reason, usage | `LLMResponse` | `chat()` |
| `_assemble_from_chunks()` | list of chunks | merges deltas + tool call fragments + usage | `LLMResponse` | `chat_stream()` |
| `_parse_tool_calls()` | raw tool_calls | JSON-parses arguments | `list[ToolCallRequest]` | `_parse_response()` |
| `from_env()` | env vars | reads `OPENAI_API_KEY/BASE/MODEL` | `OpenAICompatProvider` | `main.py:main()` |

---

## tool.py

Identical to `step4/tool.py`.

### `class ToolResult(str)`
- String subclass with `is_error` flag; factory `error()` classmethod.

### `class Tool` (ABC)
- Abstract properties: `name`, `description`, `parameters`
- `execute(**kwargs)` → `ToolResult`
- `to_schema()` → OpenAI function schema dict

### `class ToolRegistry`
- `register(tool)`, `unregister(name)`, `get(name)`, `has(name)`, `get_definitions()`, `execute(name, **params)`
- `execute()` flow: lookup → `tool.execute(**params)` → `ToolResult` (or error `ToolResult` on missing/exception)

---

## tools/echo.py

### `class EchoTool(Tool)`
- `name`: `"echo"`, `description`: `"Echoes back the input text."`
- `execute(**kwargs)`: returns `ToolResult(f"Echo: {kwargs.get('text', '')}")`

---

## runner.py

### `@dataclass AgentRunSpec`
- `initial_messages: list[dict]` — starting conversation
- `tools: ToolRegistry` — available tools
- `provider: LLMProvider` — the LLM backend
- `max_iterations: int = 10` — max tool-calling loop iterations
- `model`, `temperature`, `max_tokens` — LLM params

### `@dataclass AgentRunResult`
- `final_content: str | None` — final assistant text
- `messages: list[dict]` — full message history
- `tools_used: list[str]` — tool names in execution order
- `usage: dict[str, int]` — accumulated token counts
- `stop_reason: str` — `"stop"`, `"tool_calls"`, `"max_iterations"`, etc.
- `total_prompt_tokens` / `total_completion_tokens` [property] — convenience accessors

### `class AgentRunner`

#### `run(spec)` [async]
- **Input:** `AgentRunSpec`
- **Process:**
  1. Copies `initial_messages` as mutable message list
  2. **Loop** up to `max_iterations`:
     - Gets tool definitions from `spec.tools.get_definitions()` (or `None` if empty)
     - Calls `spec.provider.chat_with_retry(messages, tools=tools_defs, ...)`
     - Accumulates usage via `_accumulate_usage()`
     - **If response has `tool_calls` and `finish_reason == "tool_calls"`**:
       - Builds assistant message with OpenAI tool_calls format via `_build_assistant_message()`, appends to messages
       - For each `ToolCallRequest`: calls `spec.tools.execute(tc.name, **tc.arguments)`, logs tool name, appends `{"role": "tool", "tool_call_id": ..., "name": ..., "content": ...}` to messages
       - `continue` (next iteration)
     - **Else** (no tool calls): appends `{"role": "assistant", "content": ...}` to messages, returns `AgentRunResult` with `stop_reason = response.finish_reason`
  3. If loop exhausts `max_iterations`: returns `AgentRunResult` with `stop_reason = "max_iterations"` and fallback message
- **Output:** `AgentRunResult`
- **Called by:** `main.py:main()`

#### `_build_assistant_message(response)` [static]
- **Input:** `LLMResponse` with `tool_calls`
- **Process:** converts each `ToolCallRequest` to OpenAI format: `{"id": ..., "type": "function", "function": {"name": ..., "arguments": json_string}}`
- **Output:** `dict` with `role: "assistant"`, `content`, `tool_calls`

#### `_accumulate_usage(total, response)` [static]
- **Input:** running total dict + new `LLMResponse`
- **Process:** adds `prompt_tokens` and `completion_tokens` from response usage to running total

---

## main.py

### `main()`
- **Input:** command-line args (message text, defaults to `"Say 'hello' using the echo tool"`)
- **Process:**
  1. Creates `ToolRegistry`, registers `EchoTool()`
  2. Creates `OpenAICompatProvider.from_env()`
  3. Builds `AgentRunSpec` with initial user message, registry, provider, `max_iterations=5`
  4. Calls `AgentRunner().run(spec)`
  5. Prints stop_reason, final_content, accumulated token usage, and tools_used
- **Output:** stdout
- **Entry point:** `asyncio.run(main())`

---

## test.py

### `class _MockProvider`
- Returns pre-defined `LLMResponse` objects in sequence; tracks `call_count` and captured kwargs.

### `class TestAgentRunner`
- `test_direct_text_response` — LLM returns text, no tool calls → single iteration
- `test_tool_call_then_text` — LLM calls echo tool, then returns final text
- `test_tool_result_in_messages` — verifies tool result message format (`role: "tool"`, `tool_call_id`, `name`, `content`)
- `test_max_iterations` — LLM keeps requesting tools; stops at `max_iterations=2`
- `test_multiple_tool_calls_in_one_turn` — single response with 2 tool calls, both executed
- `test_tool_execution_error_propagates` — tool returning `is_error=True` still continues the loop
- `test_usage_accumulated` — token usage summed across iterations
- `test_empty_tools_no_tool_calls` — empty registry → no tool_calls in kwargs
- `test_assistant_message_tool_calls_format` — verifies OpenAI `tool_calls` structure in assistant message

---

### End-to-End Data Flow

```
CLI args ("tell me a joke")
  → main.py:main()
    → ToolRegistry.register(EchoTool())
    → OpenAICompatProvider.from_env()
    → AgentRunSpec { messages, tools, provider, max_iterations=5 }

    → AgentRunner.run(spec)
      │
      ├─ [iteration 1]
      │   provider.chat_with_retry(messages, tools=[echo schema])
      │     → OpenAICompatProvider.chat()
      │       → _build_kwargs() → AsyncOpenAI API → _parse_response()
      │       → LLMResponse(content=None, tool_calls=[ToolCallRequest("echo", {text:"..."})], finish_reason="tool_calls")
      │   → _accumulate_usage()
      │   → _build_assistant_message() → append to messages
      │   → ToolRegistry.execute("echo", text="...")
      │     → EchoTool.execute(text="...") → ToolResult("Echo: ...")
      │   → append tool result message
      │   → continue
      │
      ├─ [iteration 2]
      │   provider.chat_with_retry(messages + assistant_msg + tool_result, tools=[echo schema])
      │     → LLMResponse(content="Here's a joke...", tool_calls=[], finish_reason="stop")
      │   → _accumulate_usage()
      │   → no tool_calls → append assistant message
      │   → return AgentRunResult(final_content="Here's a joke...", stop_reason="stop", usage={...}, tools_used=["echo"])
      │
      └─ → stdout (stop_reason, final_content, token usage, tools_used)
```
