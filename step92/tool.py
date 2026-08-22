from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Callable
from copy import deepcopy
from typing import Any, TypeVar

_ToolT = TypeVar("_ToolT", bound="Tool")

_JSON_TYPE_MAP: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
}


class Schema(ABC):
    @staticmethod
    def resolve_json_schema_type(t: Any) -> str | None:
        if isinstance(t, list):
            return next((x for x in t if x != "null"), None)
        return t

    @staticmethod
    def subpath(path: str, key: str) -> str:
        return f"{path}.{key}" if path else key

    @staticmethod
    def validate_json_schema_value(val: Any, schema: dict[str, Any], path: str = "") -> list[str]:
        raw_type = schema.get("type")
        nullable = (isinstance(raw_type, list) and "null" in raw_type) or schema.get("nullable", False)
        t = Schema.resolve_json_schema_type(raw_type)
        label = path or "parameter"

        if nullable and val is None:
            return []
        if t == "integer" and (not isinstance(val, int) or isinstance(val, bool)):
            return [f"{label} should be integer"]
        if t == "number" and (
            not isinstance(val, _JSON_TYPE_MAP["number"]) or isinstance(val, bool)
        ):
            return [f"{label} should be number"]
        if t in _JSON_TYPE_MAP and t not in ("integer", "number") and not isinstance(val, _JSON_TYPE_MAP[t]):
            return [f"{label} should be {t}"]

        errors: list[str] = []
        if "enum" in schema and val not in schema["enum"]:
            errors.append(f"{label} must be one of {schema['enum']}")
        if t in ("integer", "number"):
            if "minimum" in schema and val < schema["minimum"]:
                errors.append(f"{label} must be >= {schema['minimum']}")
            if "maximum" in schema and val > schema["maximum"]:
                errors.append(f"{label} must be <= {schema['maximum']}")
        if t == "string":
            if "minLength" in schema and len(val) < schema["minLength"]:
                errors.append(f"{label} must be at least {schema['minLength']} chars")
            if "maxLength" in schema and len(val) > schema["maxLength"]:
                errors.append(f"{label} must be at most {schema['maxLength']} chars")
        if t == "object":
            props = schema.get("properties", {})
            for k in schema.get("required", []):
                if k not in val:
                    errors.append(f"missing required {Schema.subpath(path, k)}")
            additional = schema.get("additionalProperties", True)
            for k, v in val.items():
                if k in props:
                    errors.extend(Schema.validate_json_schema_value(v, props[k], Schema.subpath(path, k)))
                elif additional is False:
                    errors.append(f"unexpected parameter {Schema.subpath(path, k)}")
                elif isinstance(additional, dict):
                    errors.extend(
                        Schema.validate_json_schema_value(v, additional, Schema.subpath(path, k))
                    )
        if t == "array":
            if "minItems" in schema and len(val) < schema["minItems"]:
                errors.append(f"{label} must have at least {schema['minItems']} items")
            if "maxItems" in schema and len(val) > schema["maxItems"]:
                errors.append(f"{label} must be at most {schema['maxItems']} items")
            if "items" in schema:
                prefix = f"{path}[{{}}]" if path else "[{}]"
                for i, item in enumerate(val):
                    errors.extend(
                        Schema.validate_json_schema_value(item, schema["items"], prefix.format(i))
                    )
        return errors

    @staticmethod
    def fragment(value: Any) -> dict[str, Any]:
        to_js = getattr(value, "to_json_schema", None)
        if callable(to_js):
            return to_js()
        if isinstance(value, dict):
            return value
        raise TypeError(f"Expected schema object or dict, got {type(value).__name__}")

    @abstractmethod
    def to_json_schema(self) -> dict[str, Any]:
        ...

    def validate_value(self, value: Any, path: str = "") -> list[str]:
        return Schema.validate_json_schema_value(value, self.to_json_schema(), path)


class ToolResult(str):
    is_error: bool = False

    def __new__(cls, content: str = "", *, is_error: bool = False) -> ToolResult:
        instance = super().__new__(cls, content)
        instance.is_error = is_error
        return instance

    @classmethod
    def error(cls, content: str) -> ToolResult:
        return cls(content, is_error=True)


def is_tool_error_result(name: str, result: Any) -> bool:
    """判断工具执行结果是否为错误结果（step64：对齐 nanobot）。

    Args:
        name: 工具名（保留参数对齐 nanobot 签名，当前未使用）。
        result: 工具执行返回值。

    Returns:
        True 如果 result 是 ToolResult 且 is_error=True。
    """
    return isinstance(result, ToolResult) and result.is_error


class Tool(ABC):
    _TYPE_MAP = _JSON_TYPE_MAP
    _BOOL_TRUE = frozenset(("true", "1", "yes"))
    _BOOL_FALSE = frozenset(("false", "0", "no"))

    @staticmethod
    def _resolve_type(t: Any) -> str | None:
        return Schema.resolve_json_schema_type(t)

    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        ...

    @property
    @abstractmethod
    def parameters(self) -> dict[str, Any]:
        ...

    @property
    def read_only(self) -> bool:
        return False

    @property
    def exclusive(self) -> bool:
        return False

    @property
    def concurrency_safe(self) -> bool:
        return self.read_only and not self.exclusive

    # --- Plugin metadata ---
    config_key: str = ""
    _plugin_discoverable: bool = True
    _scopes: set[str] = {"core"}

    @classmethod
    def config_cls(cls) -> type | None:
        return None

    @classmethod
    def resolve_tool_config(cls, ctx: Any) -> Any:
        """按 `config_cls()` 从 `ctx.config.tools.<config_key>` 解析工具配置。

        step27 落地 `Tool.config_cls()`：工具在 `create(ctx)` 里调用本方法拿到
        类型化配置对象（config 缺省 / section 缺失时返回默认实例）。
        """
        cfg_cls = cls.config_cls()
        if cfg_cls is None:
            return None
        section: dict[str, Any] = {}
        if ctx is not None:
            cfg = getattr(ctx, "config", None)
            tools = getattr(cfg, "tools", None) if cfg is not None else None
            if tools is not None:
                dump = tools.model_dump()
                section = dump.get(cls.config_key or cls.__name__) or {}
        if isinstance(section, dict):
            return cfg_cls.model_validate(section)
        return section

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        return True

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls()

    def runtime_context_provider(self):
        return None

    @abstractmethod
    async def execute(self, **kwargs: Any) -> Any:
        ...

    @staticmethod
    def error(content: str) -> ToolResult:
        return ToolResult.error(content)

    # --- Parameter casting and validation ---

    def _cast_object(self, obj: Any, schema: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(obj, dict):
            return obj
        props = schema.get("properties", {})
        additional = schema.get("additionalProperties")
        casted: dict[str, Any] = {}
        for k, v in obj.items():
            if k in props:
                casted[k] = self._cast_value(v, props[k])
            elif isinstance(additional, dict):
                casted[k] = self._cast_value(v, additional)
            else:
                casted[k] = v
        return casted

    def cast_params(self, params: dict[str, Any]) -> dict[str, Any]:
        schema = self.parameters or {}
        if schema.get("type", "object") != "object":
            return params
        return self._cast_object(params, schema)

    def _cast_value(self, val: Any, schema: dict[str, Any]) -> Any:
        t = self._resolve_type(schema.get("type"))

        if t == "boolean" and isinstance(val, bool):
            return val
        if t == "integer" and isinstance(val, int) and not isinstance(val, bool):
            return val
        if t in self._TYPE_MAP and t not in ("boolean", "integer", "array", "object"):
            expected = self._TYPE_MAP[t]
            if isinstance(val, expected):
                return val

        if isinstance(val, str) and t in ("integer", "number"):
            try:
                return int(val) if t == "integer" else float(val)
            except ValueError:
                return val

        if t == "string":
            return val if val is None else str(val)

        if t == "boolean" and isinstance(val, str):
            low = val.lower()
            if low in self._BOOL_TRUE:
                return True
            if low in self._BOOL_FALSE:
                return False
            return val

        if t == "array" and isinstance(val, list):
            items = schema.get("items")
            return [self._cast_value(x, items) for x in val] if items else val

        if t == "object" and isinstance(val, dict):
            return self._cast_object(val, schema)

        return val

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        if not isinstance(params, dict):
            return [f"parameters must be an object, got {type(params).__name__}"]
        s = self.parameters or {}
        if s.get("type", "object") != "object":
            raise ValueError(f"Schema must be object type, got {s.get('type')!r}")
        return Schema.validate_json_schema_value(params, {**s, "type": "object"}, "")

    def to_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def tool_parameters(schema: dict[str, Any]) -> Callable[[type[_ToolT]], type[_ToolT]]:
    def decorator(cls: type[_ToolT]) -> type[_ToolT]:
        frozen = deepcopy(schema)

        @property
        def parameters(self: Any) -> dict[str, Any]:
            return deepcopy(frozen)

        cls.parameters = parameters

        abstract = getattr(cls, "__abstractmethods__", None)
        if abstract is not None and "parameters" in abstract:
            cls.__abstractmethods__ = frozenset(abstract - {"parameters"})
        return cls
    return decorator


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._cached_definitions: list[dict[str, Any]] | None = None

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool
        self._cached_definitions = None

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)
        self._cached_definitions = None

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        return name in self._tools

    def get_runtime_context_providers(self) -> list:
        providers: list = []
        for name in sorted(self._tools):
            provider = self._tools[name].runtime_context_provider()
            if provider is not None:
                providers.append(provider)
        return providers

    @staticmethod
    def _lookup_key(name: str) -> str:
        return "".join(ch.lower() for ch in name if ch.isalnum())

    def _suggest_name(self, name: str) -> str | None:
        key = self._lookup_key(str(name or ""))
        if not key:
            return None
        matches = [
            registered
            for registered in self._tools
            if self._lookup_key(registered) == key
        ]
        if len(matches) == 1:
            return matches[0]
        return None

    @staticmethod
    def _schema_name(schema: dict[str, Any]) -> str:
        fn = schema.get("function")
        if isinstance(fn, dict):
            name = fn.get("name")
            if isinstance(name, str):
                return name
        name = schema.get("name")
        return name if isinstance(name, str) else ""

    def get_definitions(self) -> list[dict[str, Any]]:
        if self._cached_definitions is not None:
            return self._cached_definitions
        definitions = [tool.to_schema() for tool in self._tools.values()]
        builtins: list[dict[str, Any]] = []
        mcp_tools: list[dict[str, Any]] = []
        for schema in definitions:
            name = self._schema_name(schema)
            if name.startswith("mcp_"):
                mcp_tools.append(schema)
            else:
                builtins.append(schema)
        builtins.sort(key=self._schema_name)
        mcp_tools.sort(key=self._schema_name)
        self._cached_definitions = builtins + mcp_tools
        return self._cached_definitions

    def _coerce_argument_value(self, value: Any) -> Any:
        if value is None:
            return {}
        if not isinstance(value, str):
            return value
        stripped = value.strip()
        if not stripped:
            return {}
        if not stripped.startswith(("{", "[")):
            return value
        try:
            import json
            return json.loads(stripped)
        except Exception:
            return value

    def _coerce_params(self, tool: Tool, params: Any) -> Any:
        params = self._coerce_argument_value(params)
        return self._unwrap_arguments_payload(tool, params)

    @staticmethod
    def _unwrap_arguments_payload(tool: Tool, params: Any) -> Any:
        if not isinstance(params, dict) or set(params) != {"arguments"}:
            return params
        properties = (tool.parameters or {}).get("properties", {})
        if isinstance(properties, dict) and "arguments" in properties:
            return params
        import json
        value = params.get("arguments")
        if value is None:
            return {}
        if isinstance(value, str):
            try:
                return json.loads(value)
            except Exception:
                return value
        return value or {}

    def prepare_call(self, name: str, params: Any) -> tuple[Tool | None, Any, str | None]:
        from step92.context import ContextAware, current_request_context
        tool = self._tools.get(name)
        if not tool:
            suggestion = self._suggest_name(str(name))
            hint = f" Did you mean '{suggestion}'? Tool names must match exactly." if suggestion else ""
            return None, params, (
                ToolResult.error(
                    f"Error: Tool '{name}' not found.{hint} Available: {', '.join(self.tool_names)}"
                )
            )

        if isinstance(tool, ContextAware) and (ctx := current_request_context()) is not None:
            tool.set_context(ctx)

        params = self._coerce_params(tool, params)
        if not isinstance(params, dict):
            return tool, params, (
                ToolResult.error(
                    f"Error: Tool '{name}' parameters must be a JSON object, got "
                    f"{type(params).__name__}. Use named parameters like "
                    'tool_name(param1="value1", param2="value2") matching the tool schema.'
                )
            )

        cast_params = tool.cast_params(params)
        errors = tool.validate_params(cast_params)
        if errors:
            return tool, cast_params, (
                ToolResult.error(f"Error: Invalid parameters for tool '{name}': " + "; ".join(errors))
            )
        return tool, cast_params, None

    @property
    def tool_names(self) -> list[str]:
        return list(self._tools.keys())

    async def execute(self, name: str, **params: Any) -> ToolResult:
        hint = "\n\n[Analyze the error above and try a different approach.]"
        tool, params, error = self.prepare_call(name, params)
        if error:
            return ToolResult.error(str(error) + hint)
        try:
            assert tool is not None
            result = await tool.execute(**params)
            if isinstance(result, ToolResult) and result.is_error:
                return ToolResult.error(str(result) + hint)
            return result
        except Exception as exc:
            return ToolResult.error(f"Error executing {name}: {exc}" + hint)

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools
