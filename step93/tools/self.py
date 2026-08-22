"""运行时自省工具：MyTool（step75）。

对齐 nanobot `agent/tools/self.py` 的最小子集：
- get 操作：查看运行时属性（workspace、config、工具列表等）；
- set 操作：修改允许的配置项；
- 安全机制：BLOCKED（禁止访问）、READ_ONLY（只读）、敏感字段过滤。

简化了 nanobot 的 AgentLoop 直接引用、子代理状态、MCP 状态等高级特性。
"""

from __future__ import annotations

import json
from typing import Any

from step93.schema import StringSchema, tool_parameters_schema
from step93.tool import Tool, ToolResult, tool_parameters


# ---------------------------------------------------------------------------
# 安全边界常量
# ---------------------------------------------------------------------------

# 禁止 get 和 set 的属性（核心基础设施）
_BLOCKED = frozenset({
    "bus", "provider", "runtime_resolver", "_running", "tools",
    "_runtime_vars", "runner", "sessions", "consolidator",
    "context", "commands", "_mcp_servers", "_mcp_stacks",
    "_pending_queues", "_session_locks", "_active_tasks",
    "_background_tasks", "restrict_to_workspace", "channels_config",
    "_concurrency_gate", "_unified_session", "_extra_hooks", "_hook_factories",
})

# 允许 get 但禁止 set 的属性
_READ_ONLY = frozenset({
    "workspace", "config", "iteration", "tool_count",
    "session_key", "exec_config", "web_config",
})

# 禁止访问的 Python 内部属性
_DENIED_ATTRS = frozenset({
    "__class__", "__dict__", "__bases__", "__subclasses__", "__mro__",
    "__init__", "__new__", "__reduce__", "__getstate__", "__setstate__",
    "__del__", "__call__", "__getattr__", "__setattr__", "__delattr__",
    "__code__", "__globals__", "func_globals", "func_code",
    "__wrapped__", "__closure__",
})

# 敏感字段名（值会被过滤为 "***"）
_SENSITIVE_NAMES = frozenset({
    "api_key", "secret", "password", "token", "credential",
    "private_key", "access_token", "refresh_token", "auth",
})


def _is_sensitive_name(name: str) -> bool:
    """检查字段名是否敏感（不区分大小写，包含匹配）。"""
    lower = name.lower()
    return any(s in lower for s in _SENSITIVE_NAMES)


def _safe_repr(value: Any, depth: int = 0) -> Any:
    """安全地表示值，过滤敏感字段。

    Args:
        value: 要表示的值。
        depth: 当前递归深度（防止无限递归）。

    Returns:
        可 JSON 序列化的值。
    """
    if depth > 3:
        return "..."

    if isinstance(value, dict):
        result = {}
        for k, v in value.items():
            key_str = str(k)
            if _is_sensitive_name(key_str):
                result[key_str] = "***"
            else:
                result[key_str] = _safe_repr(v, depth + 1)
        return result

    if isinstance(value, (list, tuple)):
        return [_safe_repr(v, depth + 1) for v in value[:50]]

    if hasattr(value, "__dict__") and not isinstance(value, (str, int, float, bool)):
        result = {}
        for k, v in vars(value).items():
            if k.startswith("_"):
                continue
            if _is_sensitive_name(k):
                result[k] = "***"
            else:
                result[k] = _safe_repr(v, depth + 1)
        return result if result else str(value)

    if isinstance(value, (str, int, float, bool)) or value is None:
        return value

    return str(value)


def _resolve_nested_path(obj: Any, parts: list[str]) -> Any:
    """逐段解析嵌套属性路径（step93）。

    每段都检查安全边界：
    - _DENIED_ATTRS：Python 内部属性禁止访问
    - _BLOCKED：核心基础设施禁止访问

    Args:
        obj: 起始对象。
        parts: 属性路径段列表。

    Returns:
        解析后的值，不存在时返回 None。

    Raises:
        PermissionError: 遇到禁止访问的属性。
    """
    current = obj
    for part in parts:
        if part in _DENIED_ATTRS:
            raise PermissionError(f"Access denied to internal attribute '{part}'")
        if part in _BLOCKED:
            raise PermissionError(f"Property '{part}' is blocked")
        if current is None:
            return None
        current = getattr(current, part, None)
    return current


# ---------------------------------------------------------------------------
# MyTool
# ---------------------------------------------------------------------------


@tool_parameters(
    tool_parameters_schema(
        action=StringSchema(
            "Operation: 'get' to inspect, 'set' to modify a writable property.",
            enum=["get", "set"],
        ),
        key=StringSchema("Property name to inspect or modify."),
        value=StringSchema("Value to set (required for action=set).", nullable=True),
        required=["action", "key"],
    )
)
class MyTool(Tool):
    """运行时自省工具：查看和修改 agent 运行时配置。

    安全边界：
    - BLOCKED 属性禁止访问；
    - READ_ONLY 属性禁止修改；
    - 敏感字段值被过滤为 "***"；
    - Python 内部属性禁止访问。
    """

    _scopes = {"core"}
    config_key = "my"

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        """是否启用：读取 ``config.my.enable``。"""
        return getattr(getattr(ctx.config, "my", None), "enable", True)

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        """从上下文创建工具实例。"""
        allow_set = getattr(getattr(ctx.config, "my", None), "allow_set", False)
        return cls(ctx=ctx, allow_set=allow_set)

    def __init__(self, ctx: Any = None, allow_set: bool = False):
        """初始化 MyTool。

        Args:
            ctx: ToolContext 引用（用于访问运行时状态）。
            allow_set: 是否允许 set 操作。
        """
        self._ctx = ctx
        self._allow_set = allow_set

    @property
    def name(self) -> str:
        """工具名：``my``。"""
        return "my"

    @property
    def description(self) -> str:
        """工具描述。"""
        return (
            "Inspect or modify the agent's runtime configuration. "
            "Use action='get' to view properties, action='set' to modify "
            "writable ones. Sensitive values (api_key, password, etc.) are masked."
        )

    @property
    def read_only(self) -> bool:
        """my 工具不是只读（可以修改配置）。"""
        return False

    async def execute(
        self,
        action: str | None = None,
        key: str | None = None,
        value: str | None = None,
        **kwargs: Any,
    ) -> str | ToolResult:
        """执行自省操作。

        Args:
            action: "get" 或 "set"。
            key: 属性名。
            value: set 操作的值。
            **kwargs: 忽略的额外参数。

        Returns:
            成功时返回 JSON 字符串；失败时返回 ``ToolResult.error``。
        """
        if not action:
            return ToolResult.error("Error: 'action' is required (get or set).")
        if not key:
            return ToolResult.error("Error: 'key' is required.")

        action = action.lower().strip()
        if action not in ("get", "set"):
            return ToolResult.error(f"Error: unknown action '{action}'. Use 'get' or 'set'.")

        # 安全检查：禁止的属性
        if key in _DENIED_ATTRS:
            return ToolResult.error(f"Error: access to '{key}' is denied.")
        if key in _BLOCKED:
            return ToolResult.error(f"Error: '{key}' is blocked and cannot be accessed.")

        if action == "get":
            return self._do_get(key)
        else:
            return self._do_set(key, value)

    def _do_get(self, key: str) -> str | ToolResult:
        """执行 get 操作。

        Args:
            key: 属性名或点分嵌套路径。

        Returns:
            JSON 字符串或错误。
        """
        try:
            value = self._get_runtime_value(key)
        except PermissionError as exc:
            return ToolResult.error(f"Error: {exc}")

        if value is None and not self._has_key(key):
            return ToolResult.error(f"Error: unknown property '{key}'.")

        safe_value = _safe_repr(value)
        return json.dumps({"key": key, "value": safe_value}, ensure_ascii=False, indent=2)

    def _do_set(self, key: str, value: str | None) -> str | ToolResult:
        """执行 set 操作。

        Args:
            key: 属性名。
            value: 要设置的值。

        Returns:
            JSON 字符串或错误。
        """
        if not self._allow_set:
            return ToolResult.error(
                "Error: set operations are disabled. "
                "Set config.my.allow_set=true to enable."
            )

        if key in _READ_ONLY:
            return ToolResult.error(f"Error: '{key}' is read-only and cannot be modified.")

        if value is None:
            return ToolResult.error("Error: 'value' is required for action=set.")

        # 简化版：只允许修改特定的配置项
        allowed_settable = {"exec_timeout", "web_timeout", "max_tool_result_chars"}
        if key not in allowed_settable:
            return ToolResult.error(
                f"Error: '{key}' cannot be set. "
                f"Settable properties: {', '.join(sorted(allowed_settable))}"
            )

        self._set_runtime_value(key, value)
        return json.dumps(
            {"key": key, "value": value, "status": "set"},
            ensure_ascii=False,
        )

    def _get_runtime_value(self, key: str) -> Any:
        """从运行时上下文获取属性值。

        step93 增强：支持点分嵌套路径（如 config.exec.timeout）和 agent key。

        Args:
            key: 属性名或点分路径。

        Returns:
            属性值，不存在时返回 None。

        Raises:
            PermissionError: 遇到禁止访问的属性。
        """
        ctx = self._ctx
        if ctx is None:
            return None

        # step93：点分嵌套路径
        if "." in key:
            parts = key.split(".")
            top = parts[0]
            rest = parts[1:]

            # 解析顶级对象
            if top == "config":
                base = getattr(ctx, "config", None)
            elif top == "agent":
                base = getattr(ctx, "agent_loop", None)
            elif top == "workspace":
                base = getattr(ctx, "workspace", None)
            elif top == "exec_config":
                base = getattr(getattr(ctx, "config", None), "exec", None)
            elif top == "web_config":
                base = getattr(getattr(ctx, "config", None), "web", None)
            else:
                # 尝试从 ctx 或 config 获取顶级对象
                base = getattr(ctx, top, None)
                if base is None and hasattr(ctx, "config"):
                    base = getattr(ctx.config, top, None)

            return _resolve_nested_path(base, rest)

        # 单层属性（向后兼容）
        if key == "workspace":
            return getattr(ctx, "workspace", None)
        if key == "session_key":
            return getattr(ctx, "session_key", None)
        if key == "config":
            return getattr(ctx, "config", None)
        if key == "agent":
            return getattr(ctx, "agent_loop", None)
        if key == "exec_config":
            return getattr(getattr(ctx, "config", None), "exec", None)
        if key == "web_config":
            return getattr(getattr(ctx, "config", None), "web", None)
        if key == "tool_count":
            return len(getattr(ctx, "config", None).__dict__) if ctx.config else 0
        if key == "iteration":
            return getattr(ctx, "iteration", 0)
        if key == "exec_timeout":
            return getattr(getattr(ctx.config, "exec", None), "timeout", 60)
        if key == "web_timeout":
            return getattr(getattr(ctx.config, "web", None), "timeout", 30)

        # 尝试从 config 中获取
        if hasattr(ctx, "config") and hasattr(ctx.config, key):
            return getattr(ctx.config, key)

        return None

    def _has_key(self, key: str) -> bool:
        """检查 key 是否是已知属性。

        step93 增强：支持点分嵌套路径（第一段是已知顶级 key 即可）。
        """
        # 嵌套路径：第一段是已知顶级 key 即可
        if "." in key:
            top = key.split(".")[0]
            known_tops = {
                "workspace", "session_key", "config", "agent",
                "exec_config", "web_config", "tool_count", "iteration",
                "exec_timeout", "web_timeout", "max_tool_result_chars",
            }
            return top in known_tops

        known = {
            "workspace", "session_key", "config", "agent", "exec_config", "web_config",
            "tool_count", "iteration", "exec_timeout", "web_timeout",
            "max_tool_result_chars",
        }
        return key in known

    def _set_runtime_value(self, key: str, value: str) -> None:
        """设置运行时属性值。

        Args:
            key: 属性名。
            value: 要设置的值（字符串，尝试转换为 int）。
        """
        ctx = self._ctx
        if ctx is None:
            return

        try:
            typed_value = int(value)
        except (ValueError, TypeError):
            typed_value = value

        if key == "exec_timeout":
            if hasattr(ctx.config, "exec"):
                ctx.config.exec.timeout = typed_value
        elif key == "web_timeout":
            if hasattr(ctx.config, "web"):
                ctx.config.web.timeout = typed_value
