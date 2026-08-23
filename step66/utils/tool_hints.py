"""工具调用的人读提示格式化（对齐 nanobot ``utils/tool_hints.py`` 最小集）。

把一次迭代里 LLM 请求的工具调用压成一行简洁提示（如 ``read file.md``、
``$ npm test``），供进度回调/UI 展示。只依赖工具名与参数，不执行任何东西。
"""

from __future__ import annotations

from typing import Any

# 注册表：tool_name -> (key_args, template, is_path, is_command)
_TOOL_FORMATS: dict[str, tuple[list[str], str, bool, bool]] = {
    "read_file": (["path", "file_path"], "read {}", True, False),
    "write_file": (["path", "file_path"], "write {}", True, False),
    "edit": (["file_path", "path"], "edit {}", True, False),
    "find_files": (["query", "glob", "path"], "find {}", False, False),
    "grep": (["pattern"], 'grep "{}"', False, False),
    "exec": (["command"], "$ {}", False, True),
    "list_exec_sessions": ([], "exec sessions", False, False),
    "web_search": (["query"], 'search "{}"', False, False),
    "web_fetch": (["url"], "fetch {}", True, False),
    "list_dir": (["path"], "ls {}", True, False),
}


def _truncate_middle(text: str, max_len: int) -> str:
    """把长文本中段折叠为 ``…``（保留首尾各一半）。"""
    if max_len <= 0 or len(text) <= max_len:
        return text
    half = max(max_len // 2, 3)
    return text[:half] + "…" + text[-half:]


def _get_args(tc: Any) -> dict[str, Any]:
    """从 tc.arguments 提取参数 dict（兼容 list/dict/None）。"""
    if tc.arguments is None:
        return {}
    if isinstance(tc.arguments, list):
        return tc.arguments[0] if tc.arguments else {}
    if isinstance(tc.arguments, dict):
        return tc.arguments
    return {}


def _extract_arg(tc: Any, key_args: list[str]) -> str | None:
    """按偏好键名提取第一个可用字符串值。"""
    args = _get_args(tc)
    if not isinstance(args, dict):
        return None
    for key in key_args:
        val = args.get(key)
        if isinstance(val, str) and val:
            return val
    for val in args.values():
        if isinstance(val, str) and val:
            return val
    return None


def _abbreviate_path(val: str, max_len: int) -> str:
    return _truncate_middle(val, max_len)


def _abbreviate_command(cmd: str, max_len: int) -> str:
    return _truncate_middle(cmd, max_len)


def _fmt_known(tc: Any, fmt: tuple[list[str], str, bool, bool], max_length: int) -> str:
    """用注册模板格式化已知工具。"""
    if not fmt[0] and "{}" not in fmt[1]:
        return fmt[1]
    val = _extract_arg(tc, fmt[0])
    if val is None:
        return str(getattr(tc, "name", ""))
    if fmt[2]:  # is_path
        val = _abbreviate_path(val, max_len=max_length)
    elif fmt[3]:  # is_command
        val = _abbreviate_command(val, max_len=max_length)
    return fmt[1].format(val)


def _fmt_fallback(tc: Any, max_length: int) -> str:
    """未注册工具的兜底格式：``name("value")``。"""
    name = getattr(tc, "name", "")
    args = _get_args(tc)
    val = next(iter(args.values()), None) if isinstance(args, dict) else None
    if not isinstance(val, str):
        return name
    val = _truncate_middle(val, max_length)
    return f'{name}("{val}")'


def format_tool_hints(tool_calls: list[Any], max_length: int = 40) -> str:
    """把工具调用列表格式化为简洁提示。

    Args:
        tool_calls: ToolCallRequest 列表。
        max_length: 单个提示的最大长度。

    Returns:
        逗号分隔的提示串；连续相同提示折叠为 ``hint xN``。
    """
    if not tool_calls:
        return ""

    formatted: list[str] = []
    for tc in tool_calls:
        name = getattr(tc, "name", None)
        if not isinstance(name, str) or not name:
            # 畸形工具调用（name=None）跳过，避免整个 turn 崩溃。
            continue
        fmt = _TOOL_FORMATS.get(name)
        if fmt:
            formatted.append(_fmt_known(tc, fmt, max_length))
        else:
            formatted.append(_fmt_fallback(tc, max_length))

    hints: list[tuple[str, int]] = []
    for hint in formatted:
        if hints and hints[-1][0] == hint:
            hints[-1] = (hint, hints[-1][1] + 1)
        else:
            hints.append((hint, 1))

    return ", ".join(
        f"{h} x{c}" if c > 1 else h for h, c in hints
    )
