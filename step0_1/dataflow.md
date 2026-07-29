# Step 0_1 — Data Flow

## Files: `main.py`, `test_main.py`

---

## `main.py`

### `_client() -> AsyncOpenAI`
- **Input**: reads `OPENAI_API_KEY` and `OPENAI_API_BASE` from env
- **Process**: instantiates `AsyncOpenAI(api_key, base_url)`
- **Output**: `AsyncOpenAI` client instance
- **Called by**: `call_llm()`

### `call_llm(messages: list[dict], model: str | None = None) -> dict`
- **Input**: list of message dicts (e.g., `[{"role": "user", "content": "..."}]`), optional model override
- **Process**:
  - Creates client via `_client()`
  - Resolves model from arg or `OPENAI_MODEL` env var (default `gpt-4o-mini`)
  - Calls `await client.chat.completions.create(model, messages, temperature=0.7)`
- **Output**: full API response as dict via `resp.model_dump()` (contains `choices`, `usage`, `model`, `id`)
- **Called by**: `main()`

### `main() -> None`
- **Input**: command-line args parsed via `argparse` (`message`, `--system`)
- **Process**:
  - Parses args, builds messages list (optional system message + user message)
  - Calls `await call_llm(messages)`
  - Extracts `choices[0].finish_reason`, `usage`, `choice['message']['content']`
- **Output**: prints `[finish_reason]`, token usage, and assistant content to stdout
- **Entry point**: `if __name__ == "__main__"` → `asyncio.run(main())`

### Data Flow Diagram

```
CLI: python main.py [--system <prompt>] <message>
  │
  ├── argparse: message, system
  │
  └── main()
        │
        ├── builds messages list
        │     [{"role": "system", "content": ...}] (optional)
        │     [{"role": "user", "content": ...}]
        │
        └── call_llm(messages)
              │
              ├── _client() ──> AsyncOpenAI
              │
              └── client.chat.completions.create(model, messages, temp)
                    │
                    └── resp.model_dump() ──> dict {choices, usage}
                          │
                     main() extracts:
                     finish_reason, usage, content
                          │
                      stdout (print)
```

---

## `test_main.py`

### `class TestCallLLM(unittest.IsolatedAsyncioTestCase)`
- Tests `call_llm()` with mocked OpenAI SDK

#### `setUp()`
- Sets `OPENAI_API_KEY`, clears `OPENAI_API_BASE` and `OPENAI_MODEL`

#### `tearDown()`
- Removes `OPENAI_API_KEY`

#### `_make_fake_completion(content, finish_reason) -> MagicMock`
- Builds a fake SDK response object with `.model_dump()` returning a dict
- **Called by**: test methods

#### `test_call_llm_basic(mock_sdk)`
- Mocks `AsyncOpenAI` to return a fake client
- Fake client's `chat.completions.create` is an `AsyncMock` returning fake completion
- Calls `await call_llm([{"role": "user", "content": "Hi"}])`
- **Output**: asserts `data["choices"][0]["message"]["content"] == "Hello!"`

#### `test_missing_key()`
- Removes API key
- Calls `await call_llm(...)`
- **Output**: asserts `KeyError` raised

#### `test_system_prompt(mock_sdk)`
- Mocks `AsyncOpenAI` similarly
- Calls `call_llm` with system + user messages
- Verifies SDK received 2 messages with first having `role: "system"`
- **Output**: asserts response content and SDK call arguments

### Data Flow (test)

```
test_call_llm_basic:
  mock AsyncOpenAI ──> fake client ──> call_llm([{user: "Hi"}])
    │                                     │
    │                              AsyncMock.create()
    │                                     │
    └── fake_completion.model_dump() ──> assertions

test_missing_key:
  unset env ──> call_llm([...]) ──> assertRaises(KeyError)

test_system_prompt:
  mock AsyncOpenAI ──> fake client ──> call_llm([system, user])
    │                                          │
    │                                   AsyncMock.create()
    │                                          │
    ├── assert messages[0].role == "system"
    └── assert response content == "OK"
```
