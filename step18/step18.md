# Step 18 — ToolLoader & Tool System Upgrade

在 Step 17b (Content Recovery & Continuation Control) 基础上，对齐 nanobot 的工具系统架构：引入类型化 Schema、ContextVar 上下文注入、ToolLoader 自动发现、参数校验流程。

---

## 设计原则

1. **最小增量** — 只改工具系统（tool/registry/schema/context/loader），不动 runner/loop 核心逻辑
2. **别名对齐** — 文件/类名与 nanobot 一致，import 路径 `step17b.` → `step18.`
3. **向后兼容** — `Tool` 基类只加可选方法/属性，不影响已有子类

---

## 文件变更总览

| 操作 | 文件 | 说明 |
|------|------|------|
| **新增** | `schema.py` | Schema ABC + 6 种具体类型 + `tool_parameters_schema()` |
| **新增** | `context.py` | `ToolContext`, `RequestContext`, ContextVar 绑定/重置 |
| **新增** | `loader.py` | `ToolLoader.discover()` + `load()` |
| **修改** | `tool.py` | 增强 `Tool` 基类 + 添加 `@tool_parameters` 装饰器 + `Schema` ABC |
| **修改** | `registry.py` | 新增 `prepare_call`, `_coerce_params`, `_suggest_name` |
| **修改** | `runner.py` | 工具执行前调用 `prepare_call`；绑定 `RequestContext` |
| **修改** | `loop.py` | 使用 `ToolLoader.load()` 替代手动注册 |
| **修改** | `tools/echo.py` | 改用 `@tool_parameters` + `create()` |
| **修改** | `tools/spawn.py` | 改用 `@tool_parameters` + `create()` |
| **修改** | `tools/long_task.py` | 改用 `@tool_parameters` + `create()` + ContextVar session_key |
| **修改** | `test.py` | 新增 ~200 行测试 |

---

## 技术方案

### 1. Schema 类型系统 (`schema.py`, ~150 行)

提供类型化 JSON Schema 定义，替代手写 dict。

#### `Schema` ABC（放在 `tool.py`，跟随 nanobot base.py 模式）

```python
class Schema(ABC):
    @abstractmethod
    def to_json_schema(self) -> dict[str, Any]: ...

    def validate_value(self, value: Any, path: str = "") -> list[str]:
        return Schema.validate_json_schema_value(value, self.to_json_schema(), path)

    @staticmethod
    def validate_json_schema_value(val: Any, schema: dict, path: str = "") -> list[str]: ...
    @staticmethod
    def fragment(value: Any) -> dict[str, Any]: ...
    @staticmethod
    def resolve_json_schema_type(t: Any) -> str | None: ...
    @staticmethod
    def subpath(path: str, key: str) -> str: ...
```

#### `schema.py` 具体类型

```python
from step18.tool import Schema

class StringSchema(Schema):     # min_length, max_length, enum, nullable
class IntegerSchema(Schema):    # minimum, maximum, enum, nullable
class NumberSchema(Schema):     # minimum, maximum, nullable
class BooleanSchema(Schema):    # default, nullable
class ArraySchema(Schema):      # items, min_items, max_items
class ObjectSchema(Schema):     # properties, required, additional_properties

def tool_parameters_schema(*, required=None, description="", additional_properties=False, **properties) -> dict:
    """构建根级 tool parameters schema {"type": "object", "properties": ...}"""
```

### 2. 上下文注入 (`context.py`, ~50 行)

```python
from __future__ import annotations
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any

_CURRENT_REQUEST_CONTEXT: ContextVar[RequestContext | None] = ContextVar(
    "tool_request_context", default=None,
)

@dataclass(frozen=True)
class RequestContext:
    """Per-request immutable context: set at runner entry, read by tools."""
    channel: str = ""
    chat_id: str = ""
    session_key: str | None = None
    message_id: str | None = None

@dataclass
class ToolContext:
    """Tool construction context: passed to Tool.create() and ToolLoader.load()."""
    config: Any = None
    workspace: str = ""
    bus: Any = None
    subagent_manager: Any = None
    sessions: Any = None

def current_request_context() -> RequestContext | None: ...
def current_request_session_key() -> str | None: ...
def bind_request_context(ctx: RequestContext) -> Token: ...
def reset_request_context(token: Token) -> None: ...

@contextmanager
def request_context(ctx: RequestContext):
    token = bind_request_context(ctx)
    try:
        yield ctx
    finally:
        reset_request_context(token)
```

### 3. Tool 基类增强 (`tool.py`, ~100 行新增)

在现有 step17b `Tool` 上增加：

```python
class Tool(ABC):
    # === 已有 ===
    name, description, parameters, read_only, exclusive, concurrency_safe
    execute(), to_schema()

    # === 新增插件元数据 ===
    config_key: str = ""
    _plugin_discoverable: bool = True
    _scopes: set[str] = {"core"}

    @classmethod
    def config_cls(cls) -> type | None:
        return None

    @classmethod
    def enabled(cls, ctx: ToolContext) -> bool:
        return True

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        return cls()

    def runtime_context_provider(self) -> None:
        return None

    # === 新增参数校验 ===
    def cast_params(self, params: dict[str, Any]) -> dict[str, Any]:
        """安全类型强转：str→int, "true"→True, {arguments:{...}}→展开"""
        ...

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        """JSON Schema 校验，返回错误列表（空=通过）"""
        ...

    # === 新增辅助方法 ===
    @staticmethod
    def error(content: str) -> ToolResult:
        return ToolResult(content, is_error=True)
```

新增 `@tool_parameters` 装饰器：

```python
def tool_parameters(schema: dict) -> Callable[[type[Tool]], type[Tool]]:
    """类装饰器：将 schema 附到类上，自动生成 parameters property"""
```

### 4. Registry 增强 (`registry.py`, ~60 行新增)

```python
class ToolRegistry:
    # === 已有 ===
    register(), unregister(), get(), has(), get_definitions(), execute()

    # === 新增 ===
    def prepare_call(self, name: str, params: Any) -> tuple[Tool | None, Any, ToolResult | None]:
        """解析 → 展开 → 类型强转 → 校验，返回 (tool, coerced_params, error_or_None)"""
        ...

    def get_runtime_context_providers(self) -> list: ...

    # get_definitions() 增加稳定排序 + 缓存
    # - builtins 按名排序在前
    # - mcp_ 工具在后
    # - 结果缓存到 _cached_definitions

    def _coerce_params(self, tool: Tool, params: Any) -> Any:
        """处理字符串 JSON 解析 + {arguments: ...} 展开"""
        ...

    def _suggest_name(self, name: str) -> str | None:
        """模糊名称建议（忽略大小写/非字母数字）"""
        ...
```

#### `prepare_call` 执行流程

```
1. self._tools.get(name)
   └─ 未找到 → _suggest_name() 模糊匹配 → 返回 error

2. ContextAware 协议兼容
   └─ 如果 tool 有 set_context 方法 → 注入当前 RequestContext

3. _coerce_params(tool, params)
   └─ 字符串→dict 解析
   └─ {arguments: {...}} 展开

4. tool.cast_params(casted)
   └─ str→int, str→bool 等安全强转

5. tool.validate_params(casted)
   └─ JSON Schema 校验
   └─ 错误 → 返回 ToolResult.error

6. 返回 (tool, casted_params, None)
```

### 5. ToolLoader (`loader.py`, ~80 行)

```python
_SKIP_MODULES = frozenset({
    "base", "schema", "registry", "context", "loader", "__init__",
})

class ToolLoader:
    def __init__(self, package=None, *, test_classes: list[type[Tool]] | None = None):
        """package: 默认 step18.tools"""
        ...

    def discover(self) -> list[type[Tool]]:
        """pkgutil.iter_modules → import_module → collect Tool subclasses"""
        # 跳过 _SKIP_MODULES
        # 跳过 abstract / _plugin_discoverable=False
        # 去重 (id-based)
        ...

    def load(self, ctx: ToolContext, registry: ToolRegistry, *, scope: str = "core") -> list[str]:
        """discover() → cls.create(ctx) → registry.register() → 返回注册名列表"""
        for tool_cls in self.discover():
            if scope not in tool_cls._scopes:
                continue
            if not tool_cls.enabled(ctx):
                continue
            tool = tool_cls.create(ctx)
            registry.register(tool)
        ...
```

### 6. Runner 集成 (`runner.py`, ~15 行)

#### RequestContext 绑定

```python
# AgentRunner.run() 中：
async def run(self, spec: AgentRunSpec) -> AgentRunResult:
    from step18.context import RequestContext, bind_request_context, reset_request_context

    req_ctx = RequestContext(session_key=spec.session_key)
    token = bind_request_context(req_ctx)
    try:
        result = await self._run_loop(...)
    finally:
        reset_request_context(token)
    ...
```

#### 工具执行使用 `prepare_call`

```python
# _run_tool() 中：
async def _run_tool(self, tc, spec, gov_config, hook, iter_ctx, tools_used):
    name = tc.name if hasattr(tc, 'name') else str(tc)
    tools_used.append(name)

    # 新增 prepare_call 流程
    tool, params, error = spec.tools.prepare_call(name, tc.arguments)
    if error:
        return error  # ToolResult.error，跳过执行

    result = await tool.execute(**params)
    return _GOVERNOR.normalize_tool_result(gov_config, tc.id, name, result)
```

### 7. Loop 集成 (`loop.py`, ~15 行)

```python
# _state_run() 中替换手动注册：
# 之前：
# self.registry.register(self._spawn_tool)
# self.registry.register(self._create_goal_tool)
# self.registry.register(self._update_goal_tool)

# 之后：
from step18.context import ToolContext
from step18.loader import ToolLoader

tool_ctx = ToolContext(
    config=None, workspace="",
    bus=self.bus, subagent_manager=self.subagents,
    sessions=self.sessions,
)
ToolLoader().load(tool_ctx, self.registry, scope="core")
```

移除 `self._spawn_tool` / `self._create_goal_tool` / `self._update_goal_tool` 实例属性（工具由 ToolLoader 自动管理）。

### 8. 工具类更新

#### `tools/echo.py` — 使用 `@tool_parameters`

```python
@tool_parameters(tool_parameters_schema(
    text=StringSchema("The text to echo back"),
    required=["text"],
))
class EchoTool(Tool):
    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        return cls()

    @property
    def name(self) -> str:
        return "echo"

    @property
    def description(self) -> str:
        return "Echoes back the input text."

    async def execute(self, text: str = "", **kwargs: Any) -> ToolResult:
        return ToolResult(f"Echo: {text}")
```

#### `tools/spawn.py` — 通过 `create(ctx)` 获取依赖

```python
@tool_parameters(tool_parameters_schema(
    task=StringSchema("The task for the subagent to complete"),
    label=StringSchema("Optional short label for the task"),
    required=["task"],
))
class SpawnTool(Tool):
    _scopes = {"core"}

    def __init__(self, manager: SubagentManager | None = None):
        self._manager = manager

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        return cls(manager=ctx.subagent_manager)

    @property
    def name(self) -> str: return "spawn"
    @property
    def description(self) -> str: ...
    async def execute(self, task: str = "", label: str | None = None, **kwargs) -> ToolResult: ...
```

#### `tools/long_task.py` — ContextVar 替换 `set_session_key`

```python
class CreateGoalTool(Tool):
    def __init__(self, sessions: SessionManager | None = None):
        self._sessions = sessions

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        return cls(sessions=ctx.sessions)

    async def execute(self, objective: str = "", ui_summary: str | None = None, **kwargs) -> ToolResult:
        from step18.context import current_request_context
        req = current_request_context()
        session_key = req.session_key if req else None
        if not self._sessions or not session_key:
            return ToolResult.error("Session not available.")
        sess = self._sessions.get_or_create(session_key)
        ...
```

```python
class UpdateGoalTool(Tool):
    # 同上：create(ctx) 获取 sessions，execute 中读 current_request_context().session_key
```

移除 `set_session_key()` 方法。

### 9. 测试计划 (~200 行新增)

| 测试类 | 测试数 | 说明 |
|--------|--------|------|
| `TestSchema` | 6 | String/Integer/Number/Boolean/Array/Object 的 to_json_schema + validate |
| `TestToolParameters` | 3 | `@tool_parameters` 装饰器 + `tool_parameters_schema` 构建 |
| `TestCastValidate` | 4 | `cast_params` (str→int, str→bool) + `validate_params` (类型/必填/enum 错误) |
| `TestPrepareCall` | 4 | resolve/coerce/validate/error 四条路径 |
| `TestToolContext` | 2 | RequestContext 绑定读取 + ToolContext 构造 |
| `TestToolLoader` | 3 | discover 扫描 + load 注册 + scope 过滤 |
| `TestToolLoaderIntegration` | 2 | ToolLoader → runner 端到端 |

### 10. 不做事项（推迟到后续步骤）

| 功能 | 原因 | 计划步骤 |
|------|------|----------|
| `entry_points` 插件发现 | nanobot 扩展机制，核心功能不依赖 | step21 |
| `RuntimeContextProvider` 完全实现 | 需要 system prompt 动态注入，涉及 loop 修改 | step21 |
| SSRF / workspace 安全边界 | 独立安全专项 | step20b/安全步骤 |
| `config_cls()` pydantic `BaseModel` | 保持纯 dict config 以消除依赖 | 后续步骤 |

---

## 预估工作量

| 文件 | 新增 | 修改 | 净增行 |
|------|------|------|--------|
| `schema.py` | ~150 | — | +150 |
| `context.py` | ~50 | — | +50 |
| `loader.py` | ~80 | — | +80 |
| `tool.py` | — | ~+100 | +100 |
| `registry.py` | — | ~+60 | +60 |
| `runner.py` | — | ~+15 | +15 |
| `loop.py` | — | ~+15 | -5 |
| `tools/echo.py` | — | ~+10 | +10 |
| `tools/spawn.py` | — | ~+15 | +15 |
| `tools/long_task.py` | — | ~+15 | +15 |
| `test.py` | — | ~+200 | +200 |
| **总计** | | | **~690** |
