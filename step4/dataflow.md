# Step 4 — Data Flow Documentation

## tool.py

### `class ToolResult(str)`
- **Inherits:** `str`
- **Extra field:** `is_error: bool = False`
- **`__new__(content, is_error)`:** creates a string instance with an error flag
- **`error(content)`** [classmethod]: factory for error results (`is_error=True`)
- **Input to consumers:** used as return type for all `Tool.execute()` and `ToolRegistry.execute()` calls

### `class Tool` (ABC)

- **`name`** [abstract property]: unique tool identifier (`str`)
- **`description`** [abstract property]: human-readable description (`str`)
- **`parameters`** [abstract property]: JSON Schema dict defining accepted parameters (`dict`)
- **`execute(**kwargs)`** [async]: executes the tool with given keyword arguments; returns `ToolResult`
- **`to_schema()`:** converts tool definition to OpenAI-compatible function schema:
  ```json
  {"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}
  ```
  - **Called by:** `ToolRegistry.get_definitions()`

### `class ToolRegistry`

- **`__init__()`:** initializes empty `_tools: dict[str, Tool]`
- **`register(tool)`:** stores a tool by its `name`; overwrites if name exists
- **`unregister(name)`:** removes a tool by name
- **`get(name)`:** returns `Tool | None` by name
- **`has(name)`:** checks if tool name is registered; returns `bool`
- **`get_definitions()`:** calls `tool.to_schema()` for every registered tool; returns `list[dict]`
  - **Output consumed by:** LLM provider as `tools` parameter
- **`execute(name, **params)`** [async]:
  - **Input:** tool name + keyword arguments
  - **Process:** looks up tool via `get()`, calls `tool.execute(**params)`; on missing tool or execution exception, returns `ToolResult.error(...)`
  - **Output:** `ToolResult`
  - **Called by:** `main.py:main()`

---

## tools/echo.py

### `class EchoTool(Tool)`

- **`name`:** `"echo"`
- **`description`:** `"Echoes back the input text."`
- **`parameters`:** `{"type": "object", "properties": {"text": {"type": "string", ...}}, "required": ["text"]}`
- **`execute(**kwargs)`** [async]:
  - **Input:** `kwargs` containing `text` key
  - **Process:** extracts `text`, returns `ToolResult(f"Echo: {text}")`
  - **Output:** `ToolResult`
  - **Called by:** `ToolRegistry.execute()` → `EchoTool.execute()`

---

## main.py

### `main()`
- **Input:** command-line args (`<text>` or `schemas`)
- **Process:**
  1. Creates `ToolRegistry`, registers `EchoTool()`
  2. If first arg is `"schemas"` → prints `registry.get_definitions()` as JSON
  3. Otherwise → joins remaining args as text, calls `registry.execute("echo", text=text)`
- **Output:** prints `[ok] Echo: <text>` or `[error] <message>` to stdout
- **Entry point:** `asyncio.run(main())`

---

## test.py

### `class TestToolResult`
- Tests `ToolResult` string value, `is_error` flag, `.error()` classmethod, equality, default state.

### `class TestToolABC`
- Tests that `Tool` cannot be instantiated directly, that incomplete subclasses raise `TypeError`, and that `EchoTool.to_schema()` produces correct schema.

### `class TestEchoTool`
- Tests `EchoTool.name`, `description`, `parameters` structure, `to_schema()` output.

### `class TestEchoToolAsync`
- Async tests for `EchoTool.execute()` with text and empty input.

### `class TestToolRegistry`
- Tests `register`, `get`, `has`, `get_definitions`, `execute` (found/not found), `unregister`, and overwrite behavior.

---

### End-to-End Data Flow

```
CLI args ("hello")
  → main.py:main()
    → ToolRegistry.register(EchoTool())     [stores tool]
    → ToolRegistry.execute("echo", text="hello")
      → ToolRegistry.get("echo")            [lookup]
      → EchoTool.execute(text="hello")       [execution]
      → ToolResult("Echo: hello")
  → stdout: "[ok] Echo: hello"

CLI args ("schemas")
  → main.py:main()
    → ToolRegistry.get_definitions()
      → EchoTool.to_schema()
      → [{"type": "function", "function": {...}}]
  → stdout: JSON pretty-print
```
