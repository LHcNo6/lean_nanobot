# Step 1 — Data Flow

## Files: `llm.py`, `main.py`, `test.py`

---

## `llm.py`

### `@dataclass ToolCallRequest`
- **Fields**: `id` (str), `name` (str), `arguments` (Any)
- Used by: `LLMResponse.tool_calls`, returned from `OpenAICompatProvider.chat()`

### `@dataclass LLMResponse`
- **Fields**: `content` (str\|None), `tool_calls` (list[ToolCallRequest], default=[]), `finish_reason` (str, default="stop"), `usage` (dict[str,int], default={})
- Return type of all `chat()` methods

### `class LLMProvider(ABC)`
- Abstract base class

#### `chat(messages, tools, model, temperature, max_tokens) -> LLMResponse`
- Abstract method
- **Input**: `messages: list[dict]`, optional `tools: list[dict]`, `model`, `temperature`, `max_tokens`
- **Output**: `LLMResponse` (contract)
- **Subclassed by**: `OpenAICompatProvider`

### `class OpenAICompatProvider(LLMProvider)`

#### `__init__(api_key, api_base, model)`
- **Input**: API key, base URL, default model name
- **Process**: creates `AsyncOpenAI` client stored as `self._client`
- **Called by**: `from_env()`, tests

#### `chat(messages, tools, model, temperature, max_tokens) -> LLMResponse`
- **Input**: message list, optional tool definitions, model override, temperature, max_tokens
- **Process**:
  - Calls `self._client.chat.completions.create(...)` with the OpenAI SDK
  - Parses response:
    - Extracts `msg.content`, `msg.tool_calls`
    - Converts tool calls: parses JSON arguments → `ToolCallRequest` list
    - Extracts `finish_reason`, `usage` dict
- **Output**: `LLMResponse(content, tool_calls, finish_reason, usage)`
- **Called by**: `main()` (`main.py:28`)

#### `from_env() -> OpenAICompatProvider` (classmethod)
- **Input**: reads env vars `OPENAI_API_KEY`, `OPENAI_API_BASE`, `OPENAI_MODEL`
- **Process**: calls `cls(api_key, api_base, model)`
- **Output**: `OpenAICompatProvider` instance
- **Called by**: `main()` (`main.py:27`)

---

## `main.py`

### `main() -> None`
- **Input**: command-line args via `sys.argv` (`<message>`, `--system <prompt>`)
- **Process**:
  - Parses message and optional system prompt from `sys.argv`
  - Builds messages list: `[{"role": "system", ...}]` (optional) + `[{"role": "user", ...}]`
  - Creates provider: `OpenAICompatProvider.from_env()`
  - Calls `await provider.chat(messages)`
  - Extracts `resp.finish_reason`, `resp.usage`, `resp.content`
- **Output**: prints `[finish_reason]`, token usage, assistant content to stdout
- **Entry point**: `if __name__ == "__main__"` → `asyncio.run(main())`

### Data Flow Diagram

```
CLI: python -m step1.main <message> [--system <prompt>]
  │
  └── main()
        │
        ├── sys.argv ──> message, system
        ├── builds messages list
        │
        ├── OpenAICompatProvider.from_env()
        │     ├── env: OPENAI_API_KEY
        │     ├── env: OPENAI_API_BASE
        │     └── env: OPENAI_MODEL
        │
        └── provider.chat(messages)
              │
              ├── OpenAI SDK: client.chat.completions.create(...)
              │
              └── _parse_response()
                    ├── msg.content ──> LLMResponse.content
                    ├── msg.tool_calls ──> ToolCallRequest[] ──> LLMResponse.tool_calls
                    ├── choice.finish_reason ──> LLMResponse.finish_reason
                    └── resp.usage ──> LLMResponse.usage
                          │
                    main() extracts:
                    resp.finish_reason, resp.usage, resp.content
                          │
                      stdout (print)
```

---

## `test.py`

### `class TestDataTypes(unittest.TestCase)`
- Tests dataclass construction

#### `test_tool_call_request()` → validates `ToolCallRequest` fields
#### `test_llm_response()` → validates `LLMResponse` defaults (`tool_calls` is falsy)
#### `test_llm_response_with_tools()` → validates `LLMResponse` with tool calls

### `class TestLLMProvider(unittest.TestCase)`

#### `test_abc_cannot_instantiate()`
- Asserts `TypeError` when directly instantiating `LLMProvider()`

### `class TestOpenAICompatProvider(unittest.IsolatedAsyncioTestCase)`

#### `setUp()` / `tearDown()`
- Sets/clears `OPENAI_API_KEY` env var

#### `test_chat_basic(mock_sdk)`
- Mocks `AsyncOpenAI` → fake client → fake SDK response message
- Calls `provider.chat([{"role": "user", "content": "Hi"}])`
- **Output**: asserts `resp.content == "Hello!"`, `resp.finish_reason == "stop"`, `resp.usage["prompt_tokens"] == 10`

#### `test_chat_with_tool_calls(mock_sdk)`
- Mocks response with `msg.tool_calls` containing a tool call delta
- Calls `provider.chat(...)`
- **Output**: asserts `resp.content is None`, `resp.finish_reason == "tool_calls"`, `resp.tool_calls[0].name == "echo"`

#### `test_chat_with_tools_param(mock_sdk)`
- Mocks response, passes `tools` kwarg to `provider.chat()`
- **Output**: asserts `resp.content == "OK"` (verifies tools kwarg propagated)

#### `test_from_env_missing_key()`
- Removes API key env var
- Calls `OpenAICompatProvider.from_env()`
- **Output**: asserts `KeyError` raised

### Data Flow (test)

```
test_chat_basic:
  mock AsyncOpenAI ──> fake SDK response ──> provider.chat([{user:"Hi"}])
                                              │
                                        _parse_response()
                                              │
                                        LLMResponse ──> assertions

test_chat_with_tool_calls:
  mock AsyncOpenAI ──> fake SDK response (with tool_calls) ──> provider.chat()
                                                                │
                                                          LLMResponse.tool_calls
                                                                │
                                                          assertions on name, arguments

test_chat_with_tools_param:
  mock AsyncOpenAI ──> provider.chat(messages, tools=tools_def)
                          │
                    SDK receives kwargs["tools"] = tools_def
                          │
                    assertions
```
