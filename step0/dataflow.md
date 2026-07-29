# Step 0 — Data Flow

## Files: `main.py`, `test_main.py`

---

## `main.py`

### `call_llm(message: str) -> dict`
- **Input**: user message string from `sys.argv[1]`
- **Process**:
  - Reads env vars `OPENAI_API_KEY`, `OPENAI_API_BASE`, `OPENAI_MODEL`
  - Builds JSON body `{"model", "messages", "temperature"}`
  - Sends HTTP POST to `{api_base}/chat/completions` with `Authorization: Bearer {api_key}`
- **Output**: parsed JSON dict from API response (contains `choices`, `usage`)
- **Called by**: `main()`

### `main() -> None`
- **Input**: command-line args via `sys.argv`
- **Process**:
  - Validates `sys.argv` length
  - Calls `call_llm(sys.argv[1])`
  - Extracts `choices[0].finish_reason`, `usage`, `choice['message']['content']`
- **Output**: prints `[finish_reason]`, token usage, and assistant content to stdout
- **Entry point**: `if __name__ == "__main__"` → `main()`

### Data Flow Diagram

```
CLI args ──> main() ──> call_llm(message)
                          │
                   ┌──────┴──────┐
                   │  HTTP POST  │─── env: OPENAI_API_KEY, etc.
                   │  to API     │
                   └──────┬──────┘
                          │
                   parsed JSON dict
                          │
                     main() extracts:
                     finish_reason, usage, content
                          │
                      stdout (print)
```

---

## `test_main.py`

### `class TestCallLLM(unittest.TestCase)`
- Tests `call_llm()` with mocked HTTP

#### `setUp()`
- Sets `OPENAI_API_KEY` env var

#### `tearDown()`
- Removes `OPENAI_API_KEY` env var

#### `test_call_llm_success(mock_urlopen)`
- Builds fake JSON response dict
- Mocks `urllib.request.urlopen` to return fake response
- Calls `call_llm("Hi")`
- **Input**: mocked HTTP layer
- **Output**: asserts `data["choices"][0]["message"]["content"] == "Hello there!"`

#### `test_call_llm_missing_key()`
- Removes API key from env
- Calls `call_llm("Hi")`
- **Output**: asserts `RuntimeError` raised

### `class TestMain(unittest.TestCase)`

#### `test_main_success(mock_call_llm)`
- Mocks `step0.main.call_llm` to return a fake response
- Mocks `sys.argv` to `["main.py", "hello"]`
- Calls `main()`
- **Output**: verifies main prints without raising (implicit assertion via no exception)

### Data Flow (test)

```
test_call_llm_success:
  mock_urlopen ──> fake HTTP response ──> call_llm("Hi") ──> assertions on parsed dict

test_call_llm_missing_key:
  unset env ──> call_llm("Hi") ──> assertRaises(RuntimeError)

test_main_success:
  mock call_llm ──> fake response dict ──> main() ──> prints output (no exception)
```
