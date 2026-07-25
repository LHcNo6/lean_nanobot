# Step 4 — Tool 基类 + Registry

## 目标

定义 `Tool(ABC)` 基类、`ToolResult` 返回值类型、`ToolRegistry` 注册中心，以及一个 `EchoTool` 示例工具。

## 文件结构

```
step4/
├── __init__.py
├── tool.py          # Tool(ABC) + ToolResult + ToolRegistry（对应 nanobot base.py + registry.py）
├── tools/
│   ├── __init__.py
│   └── echo.py      # EchoTool
├── main.py          # 命令行验证
├── test.py          # 15 个测试
└── step4.md         # 本文档
```

## 核心接口

### ToolResult

`str` 子类，带有 `is_error` 标志：

```python
class ToolResult(str):
    is_error: bool = False
    @classmethod
    def error(cls, content: str) -> ToolResult: ...
```

用法：
```python
r = ToolResult("hello")          # 普通结果
r = ToolResult("err", is_error=True)  # 错误结果
r = ToolResult.error("失败")      # 工厂方法
```

### Tool(ABC)

最小基类，只保留 LLM 交互所需的核心字段：

```python
class Tool(ABC):
    @property @abstractmethod
    def name(self) -> str: ...
    @property @abstractmethod
    def description(self) -> str: ...
    @property @abstractmethod
    def parameters(self) -> dict: ...  # JSON Schema
    async def execute(self, **kwargs) -> ToolResult: ...
    def to_schema(self) -> dict: ...   # → OpenAI tool format
```

### ToolRegistry

```python
class ToolRegistry:
    def register(self, tool: Tool) -> None: ...
    def unregister(self, name: str) -> None: ...
    def get(self, name: str) -> Tool | None: ...
    def has(self, name: str) -> bool: ...
    def get_definitions(self) -> list[dict]: ...  # → list[OpenAI tool schema]
    async def execute(self, name: str, **params) -> ToolResult: ...
```

### EchoTool

```python
class EchoTool(Tool):
    name = "echo"
    description = "Echoes back the input text."
    parameters = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "The text to echo back"},
        },
        "required": ["text"],
    }
    async def execute(self, **kwargs) -> ToolResult:
        return ToolResult(f"Echo: {kwargs.get('text', '')}")
```

## 与 nanobot 对比

| 功能 | nanobot | step4 | 原因 |
|---|---|---|---|
| `ToolResult` | str 子类 + is_error | 完全相同 | 设计合理，直接复用 |
| `Tool` 字段 | name, desc, params + cast/validate + 插件元数据 | 仅 name, desc, params, execute | Step 5 才需要校验 |
| Schema 类型 | 6 种 Schema 类 + 校验 | 纯 dict（JSON Schema） | 复杂 schema 引擎留到需要时 |
| `ToolRegistry` | register + get_definitions + prepare_call + execute | register + get + get_definitions + execute | prepare_call 在 AgentRunner 做 |
| `ToolLoader` | pkgutil 自动发现 | 手动 register | 1 个工具不需要自动发现 |
| `ToolContext` | 配置/工作区/总线 | 无 | Step 6 引入上下文时再处理 |

## 测试覆盖（15 个）

| 分类 | 测试数 | 内容 |
|---|---|---|
| ToolResult | 5 | str 值、is_error、error()、默认值、空构造 |
| Tool ABC | 3 | 不能直接实例化、不完整子类报错、to_schema 格式 |
| EchoTool | 5 | name、description、parameters 结构、execute 正常/空、to_schema |
| ToolRegistry | 5 | register/get/has、get_definitions、execute、execute 找不到、unregister、覆盖注册 |

## 暴露的问题

1. **参数校验** — 目前 execute 直接 `**kwargs`，没有 JSON Schema 校验。LLM 可能传参错误（Step 5 AgentRunner 加校验）
2. **无工具返回格式约束** — ToolResult 只是字符串，LLM 无法区分结构化的 vs 流式的（后续可加 `ToolResult(format=...)`）
3. **一个文件多职责** — `tool.py` 同时包含 ABC、ToolResult、Registry。后续可像 nanobot 一样拆分

## 下一 Step 方向

**Step 5：AgentRunner 单轮工具** — 把 ToolRegistry 接入 LLM 调用：发送 tools → 接收 tool_calls → 执行 → 回传结果 → 重复直到 LLM 返回文本。
