# Step 2 — Data Flow

## Files: `llm.py`, `main.py`, `test.py`

---

## `llm.py`

### Data Types

#### `@dataclass ToolCallRequest`
- **Fields**: `id` (str), `name` (str), `arguments` (Any)

#### `@dataclass LLMResponse`
- **Fields**: `content` (str\|None), `tool_calls` (list[ToolCallRequest], default=[]), `finish_reason` (str, default="stop"), `usage` (dict[str,int], default={})

### `class LLMProvider(ABC)`

#### `chat(messages, tools, model, temperature, max_tokens) -> LLMResponse`
- Abstract method — same contract as Step 1

#### `chat_stream(messages, tools, model, temperature, max_tokens, on_content_delta) -> LLMResponse`
- **Input**: messages, optional tools/model/temperature/max_tokens, optional `on_content_delta` callback
- **Process (default fallback)**:
  - Calls `self.chat(...)` (non-streaming)
  - If `on_content_delta` is provided and response has content, calls `await on_content_delta(response.content)`
- **Output**: `LLMResponse` (same as `chat()`)
- **Override**: `OpenAICompatProvider` overrides this with native SSE streaming

### `class OpenAICompatProvider(LLMProvider)`

#### `__init__(api_key, api_base, model)`
- Creates `AsyncOpenAI` client, stores default model

#### `chat(messages, tools, model, temperature, max_tokens) -> LLMResponse`
- **Input**: message list, optional tools
- **Process**:
  - Calls `self._build_kwargs(...)` to get request kwargs
  - Calls `self._client.chat.completions.create(**kwargs)` (non-streaming)
  - Calls `self._parse_response(resp)` to convert SDK response → `LLMResponse`
- **Output**: `LLMResponse`
- **Called by**: `main()` (non-stream path)

#### `chat_stream(messages, tools, model, temperature, max_tokens, on_content_delta) -> LLMResponse`
- **Input**: messages, optional tools, callback `on_content_delta(text)`
- **Process**:
  - Calls `self._build_kwargs(...)` and adds `stream=True`, `stream_options={"include_usage": True}`
  - Iterates SSE chunks via `asyncio.wait_for` (timeout: 30s per chunk)
  - For each chunk: appends to list, calls `await on_content_delta(delta.content)` if present
  - On `asyncio.TimeoutError`: returns `LLMResponse(content=None, finish_reason="error")`
  - On `StopAsyncIteration`: calls `self._assemble_from_chunks(chunks)` to build final response
- **Output**: `LLMResponse`
- **Called by**: `main()` (stream path)

#### `_build_kwargs(messages, tools, model, temperature, max_tokens) -> dict`
- Builds the kwargs dict for OpenAI SDK calls
- **Called by**: `chat()`, `chat_stream()`

#### `_parse_response(resp) -> LLMResponse`
- Parses a non-streaming SDK response into `LLMResponse`
- **Called by**: `chat()`
- Uses `_parse_tool_calls()` internally

#### `_assemble_from_chunks(chunks) -> LLMResponse`
- Accumulates content/tool calls from streaming chunks
- **Input**: list of chunk objects
- **Process**:
  - Iterates chunks: concatenates `delta.content`, tracks `finish_reason`, accumulates tool call deltas by index (merges `id`, `name`, `arguments` across chunks)
  - Parses accumulated JSON arguments string → dict
  - Extracts `usage` from last chunk
- **Output**: `LLMResponse`
- **Called by**: `chat_stream()`

#### `_parse_tool_calls(tool_calls_raw) -> list[ToolCallRequest]` (static)
- Parses raw tool call objects into `ToolCallRequest` list
- **Called by**: `_parse_response()`

#### `from_env() -> OpenAICompatProvider` (classmethod)
- Reads env vars `OPENAI_API_KEY`, `OPENAI_API_BASE`, `OPENAI_MODEL`
- Returns `OpenAICompatProvider` instance

---

## `main.py`

### `main() -> None`
- **Input**: command-line args (`<message>`, `--system <prompt>`, `--stream`)
- **Process**:
  - Parses `sys.argv` for message, system prompt, stream flag
  - Builds messages list
  - Creates provider: `OpenAICompatProvider.from_env()`
  - **If `--stream`**:
    - Defines `on_delta(text)` callback that prints text inline
    - Calls `await provider.chat_stream(messages, on_content_delta=on_delta)`
    - Prints newline, then `[finish_reason]`, usage
  - **If not `--stream`**:
    - Calls `await provider.chat(messages)`
    - Prints `[finish_reason]`, usage, content
- **Output**: printed to stdout
- **Entry point**: `if __name__ == "__main__"` → `asyncio.run(main())`

### Data Flow Diagram

```
CLI: python -m step2.main <message> [--system <prompt>] [--stream]
  │
  └── main()
        │
        ├── sys.argv ──> message, system, stream
        ├── builds messages list
        ├── OpenAICompatProvider.from_env()
        │
        ├── if --stream:
        │     │
        │     ├── on_delta(text) ──> print(text, end="")
        │     │
        │     └── provider.chat_stream(messages, on_content_delta)
        │           │
        │           ├── _build_kwargs() + stream=True
        │           ├── AsyncOpenAI SDK ──> SSE chunk stream
        │           │
        │           ├── for each chunk:
        │           │   ├── asyncio.wait_for(chunk, timeout=30s)
        │           │   ├── chunks.append(chunk)
        │           │   └── on_content_delta(delta.content)
        │           │
        │           ├── on TimeoutError: return LLMResponse(finish_reason="error")
        │           │
        │           └── _assemble_from_chunks(chunks)
        │                 │
        │                 ├── concatenate delta.content ──> full_content
        │                 ├── accumulate tool_call deltas ──> ToolCallRequest[]
        │                 ├── extract usage from last chunk
        │                 └── return LLMResponse
        │
        └── if not --stream:
              │
              └── provider.chat(messages)
                    │
                    ├── _build_kwargs()
                    ├── OpenAI SDK (non-streaming)
                    └── _parse_response() ──> LLMResponse
                          │
                    print finish_reason, usage, content
```

---

## `test.py`

### Helper Classes/Functions

#### `class MockStream`
- `__init__(chunks)`: stores pre-defined chunk list
- `__aiter__()` / `__anext__()`: async iterator yielding chunks on demand

#### `_chunk_choice(delta_content, finish_reason, tool_calls) -> MagicMock`
- Builds a single `ChatCompletionChunk.choices[0]` mock with delta content/tool_calls/finish

#### `_chunk(content, finish, usage, tool_calls) -> MagicMock`
- Builds a full `ChatCompletionChunk` mock with choices and optional usage

### `class TestDataTypes(unittest.TestCase)`
- `test_tool_call_request()`: validates `ToolCallRequest` construction
- `test_llm_response()`: validates `LLMResponse` defaults

### `class TestLLMProviderBaseStream(unittest.IsolatedAsyncioTestCase)`

#### `test_default_fallback_calls_chat()`
- Creates `DummyProvider` that only implements `chat()`
- Calls `provider.chat_stream(...)` with `on_content_delta`
- **Output**: asserts `resp.content == "mock response"`, deltas == `["mock response"]`

#### `test_default_no_delta_when_no_content()`
- DummyProvider's `chat()` returns `content=None`
- Calls `chat_stream(...)`
- **Output**: asserts `on_delta` was never called

### `class TestOpenAICompatProviderStream(unittest.IsolatedAsyncioTestCase)`

#### `setUp()` / `tearDown()` — manage env

#### `test_stream_text_content(mock_sdk)`
- Creates chunks: `"Hello"`, `" world"`, `"!"` (with `finish="stop"` and usage)
- Mocks `AsyncOpenAI` → fake client → `MockStream`
- Calls `provider.chat_stream(...)` with `on_delta` collector
- **Output**: asserts deltas = `["Hello", " world", "!"]`, `resp.content == "Hello world!"`, `resp.finish_reason == "stop"`, usage tokens correct

#### `test_stream_tool_calls(mock_sdk)`
- Creates chunks with tool call deltas (two chunks: partial `{"text":` + ` "hi"}`)
- Calls `provider.chat_stream(...)` without `on_delta`
- **Output**: asserts `resp.tool_calls[0].name == "echo"`, `arguments["text"] == "hi"`

#### `test_stream_timeout(mock_sdk)`
- Creates `TimeoutStream` that raises `asyncio.TimeoutError`
- Calls `provider.chat_stream(...)`
- **Output**: asserts `resp.finish_reason == "error"`, `resp.content is None`

#### `test_stream_empty_chunks(mock_sdk)`
- Creates a chunk with empty choices and usage
- Calls `provider.chat_stream(...)`
- **Output**: asserts `resp.content is None`, `resp.finish_reason == "stop"`

### Data Flow (test)

```
test_stream_text_content:
  mock AsyncOpenAI ──> MockStream(["Hello", " world", "!"])
                        │
                  provider.chat_stream(..., on_delta)
                        │
                  _assemble_from_chunks(chunks)
                        │
                  LLMResponse(content="Hello world!", ...)
                        │
                  assert deltas == ["Hello", " world", "!"]
                  assert content, finish_reason, usage

test_stream_tool_calls:
  mock AsyncOpenAI ──> MockStream(tool_call_chunks)
                        │
                  provider.chat_stream(...)
                        │
                  _assemble_from_chunks()
                  (accumulates partial JSON args)
                        │
                  LLMResponse(tool_calls=[ToolCallRequest("echo", {"text":"hi"})])
                        │
                  assertions on tool_calls[0]

test_stream_timeout:
  mock AsyncOpenAI ──> TimeoutStream (raises TimeoutError)
                        │
                  provider.chat_stream(...)
                        │
                  return LLMResponse(finish_reason="error")
                        │
                  assertions

test_default_fallback_calls_chat:
  DummyProvider.chat() ──> LLMResponse(content="mock response")
                        │
                  LLMProvider.chat_stream() fallback
                        │
                  on_content_delta("mock response")
                  return LLMResponse
```
