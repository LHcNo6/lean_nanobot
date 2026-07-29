# Step 3 — Data Flow Documentation

## llm.py

### `@dataclass ToolCallRequest`
- Fields: `id: str`, `name: str`, `arguments: Any`
- Pure data container for LLM tool call requests.

### `@dataclass LLMResponse`
- Fields: `content: str | None`, `tool_calls: list[ToolCallRequest]`, `finish_reason: str`, `usage: dict[str, int]`
- Universal response type returned by all provider methods.

### `@dataclass RetryConfig`
- Fields: `max_retries: int = 3`, `base_delay: float = 1.0`, `max_delay: float = 60.0`, `retry_mode: str = "standard"`
- Configuration for retry/backoff behavior.

---

## provider.py

### `_is_retryable_exception(exc)`
- **Input:** an `Exception` instance
- **Process:** checks if the exception is an `asyncio.TimeoutError`, `openai.APIConnectionError`, `openai.APITimeoutError`, `openai.RateLimitError`, `openai.InternalServerError`, or any 5xx `APIStatusError`
- **Output:** `bool`
- **Called by:** `LLMProvider.chat_with_retry()`, `LLMProvider.chat_stream_with_retry()`

### `_backoff_delay(attempt, config)`
- **Input:** attempt index (`int`), `RetryConfig`
- **Process:** computes `min(base_delay * 2^attempt, max_delay)` then jitters by multiplying with `0.5 + random()`
- **Output:** `float` (seconds to sleep)
- **Called by:** `LLMProvider.chat_with_retry()`, `LLMProvider.chat_stream_with_retry()`

### `class _StreamGuard`
- Simple flag holder: `delta_delivered: bool = False`
- Used to detect whether any content delta was delivered before a stream failure.

### `class LLMProvider` (ABC)

#### `chat(messages, tools, model, temperature, max_tokens)` [abstract]
- **Input:** message list, optional tool schemas, model name, temperature, max_tokens
- **Output:** `LLMResponse`
- **Called by:** `chat_with_retry()`, `chat_stream()`

#### `chat_stream(messages, tools, model, temperature, max_tokens, on_content_delta)`
- **Input:** same as `chat()` plus optional `on_content_delta` callback
- **Process:** calls `self.chat()`; if `on_content_delta` is set and response has content, invokes it
- **Output:** `LLMResponse`
- **Called by:** `chat_stream_with_retry()`

#### `chat_with_retry(messages, tools, model, temperature, max_tokens, retry_config)`
- **Input:** same as `chat()` plus optional `RetryConfig`
- **Process:** loop calling `self.chat()`; on retryable exception, computes backoff via `_backoff_delay()` and sleeps; raises on non-retryable, cancellation, or retry exhaustion
- **Output:** `LLMResponse`
- **Called by:** `main.py:main()`

#### `chat_stream_with_retry(messages, tools, model, temperature, max_tokens, on_content_delta, retry_config)`
- **Input:** same as `chat_stream()` plus optional `RetryConfig`
- **Process:** same retry loop as `chat_with_retry()` but calls `self.chat_stream()`; uses `_StreamGuard` to avoid retrying if any delta was already delivered to the caller
- **Output:** `LLMResponse`
- **Called by:** `main.py:main()`

---

## openai_compat_provider.py

### `class OpenAICompatProvider(LLMProvider)`

#### `__init__(api_key, api_base, model)`
- **Input:** API key, base URL, model name
- **Process:** creates `AsyncOpenAI` client with `max_retries=0` (retry handled by provider layer)
- **Output:** None

#### `chat(messages, tools, model, temperature, max_tokens)`
- **Input:** standard LLMProvider params
- **Process:** calls `_build_kwargs()` then `AsyncOpenAI.chat.completions.create()`, then `_parse_response()`
- **Output:** `LLMResponse`
- **Called by:** `LLMProvider.chat_with_retry()` (inherited)

#### `chat_stream(messages, tools, model, temperature, max_tokens, on_content_delta)`
- **Input:** standard LLMProvider params plus delta callback
- **Process:** calls `_build_kwargs()` with `stream=True`, iterates with `asyncio.wait_for()` (30s timeout per chunk), invokes `on_content_delta` per chunk, then `_assemble_from_chunks()`
- **Output:** `LLMResponse`
- **Called by:** `LLMProvider.chat_stream_with_retry()` (inherited)

#### `_build_kwargs(messages, tools, model, temperature, max_tokens)`
- **Input:** raw params
- **Process:** assembles dict for OpenAI API; includes `tools` if present
- **Output:** `dict[str, Any]`
- **Called by:** `chat()`, `chat_stream()`

#### `_parse_response(resp)`
- **Input:** raw OpenAI API response object
- **Process:** extracts content, tool_calls (via `_parse_tool_calls`), finish_reason, usage tokens
- **Output:** `LLMResponse`
- **Called by:** `chat()`

#### `_assemble_from_chunks(chunks)`
- **Input:** list of raw stream chunks
- **Process:** concatenates content deltas, merges tool call fragments (by index), extracts finish_reason and usage from last chunk; parses arguments JSON
- **Output:** `LLMResponse`
- **Called by:** `chat_stream()`

#### `_parse_tool_calls(tool_calls_raw)` [static]
- **Input:** raw tool calls from OpenAI response
- **Process:** parses `id`, `function.name`, `function.arguments` (JSON-deserialized)
- **Output:** `list[ToolCallRequest]`
- **Called by:** `_parse_response()`

#### `from_env()` [classmethod]
- **Input:** environment variables `OPENAI_API_KEY`, `OPENAI_API_BASE`, `OPENAI_MODEL`
- **Process:** reads .env via `dotenv.load_dotenv()`, constructs instance
- **Output:** `OpenAICompatProvider`
- **Called by:** `main.py:main()`

---

## main.py

### `main()`
- **Input:** command-line args (`<message> [--system <prompt>] [--stream] [--retry]`)
- **Process:**
  1. Parses CLI args into message list (system + user)
  2. Creates `OpenAICompatProvider.from_env()`
  3. If `--stream` and `--retry` → `provider.chat_stream_with_retry()`
  4. If `--stream` only → `provider.chat_stream()`
  5. If `--retry` only → `provider.chat_with_retry()`
  6. Otherwise → `provider.chat()`
  7. Prints streaming deltas live, then final content, finish_reason, and token usage
- **Output:** prints to stdout
- **Entry point:** `asyncio.run(main())`

---

## test.py

### `_fake_request()`
- Creates a dummy `httpx.Request` for test exception construction.

### `_fake_response(status_code)`
- Creates a dummy `httpx.Response` with given status code.

### `class _RetryTestProvider(LLMProvider)`
- Mock provider that fails on demand based on pre-configured exception lists for `chat` and `chat_stream`.

### `class _MockStream`
- Async iterator over pre-defined chunks, used to simulate OpenAI streaming.

### `_chunk(content, finish, usage)`
- Factory that builds a mock stream chunk with a content delta.

### `class TestIsRetryableException`
- Unit tests for `_is_retryable_exception()` covering all retryable and non-retryable exception types.

### `class TestChatWithRetry`
- Tests `chat_with_retry()`: success path, fail-then-succeed, exhaustion, non-retryable propagation.

### `class TestChatStreamWithRetry`
- Tests `chat_stream_with_retry()`: success, fail before delta (retried), fail after delta (no retry), exhaustion, timeout before delta.

### `class TestOpenAICompatRetry`
- Integration tests via mocked `AsyncOpenAI` SDK testing connection error retry, streaming timeout retry, and non-retryable auth error propagation.

---

### End-to-End Data Flow

```
CLI args
  → main.py main()
    → OpenAICompatProvider.from_env()  [reads .env]
    → LLMProvider.chat_with_retry() / chat_stream_with_retry()  [retry loop]
      → OpenAICompatProvider.chat() / chat_stream()  [SDK call]
        → _build_kwargs() → AsyncOpenAI API → _parse_response() / _assemble_from_chunks()
        → LLMResponse  ◄─── llm.py data classes
  → stdout (content, finish_reason, usage)
```
