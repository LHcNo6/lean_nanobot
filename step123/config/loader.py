"""Configuration loading utilities.

对齐 nanobot `config/loader.py` 的最小子集（H1）：
- `load_config(path)`：无文件 → 全默认；有文件 → `_migrate_config` → `Config.model_validate`；
- `save_config(config, path)`：`model_dump(mode="json", by_alias=True)` 落盘
  （`${VAR}` 模板保留在文件里，不展开写入）；
- `resolve_config_env_vars(config)`：原地递归把 `${VAR}` 替换为环境变量值，
  未设置时抛 `ValueError`（对齐 nanobot，api_key 可写 `${OPENAI_API_KEY}` 模板）；
- `_env_to_config_dict()`：手写 `NANOBOT_` 前缀 env 解析（`__` 嵌套分隔符），
  与文件数据 `_merge_file_and_env` 合并：**文件优先、env 只补缺省**。

与 nanobot 的差异：
- nanobot 用 `pydantic_settings.BaseSettings` 在构造 Config() 时读 env（文件存在时
  model_validate 反而忽略 env）；lean 手写解析并把语义明确为 "文件优先、env 补缺"，
  两层来源都是纯数据 dict，可独立测试、可读性强。
- `set_config_path` / `get_config_path`：全局配置路径（多实例/测试用）。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import pydantic
from pydantic import BaseModel

from step123.config.schema import Config

# 全局当前配置路径（多实例 / 测试注入用）
_current_config_path: Path | None = None

_ENV_PREFIX = "NANOBOT_"
_ENV_NESTED_DELIMITER = "__"

_ENV_REF_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def set_config_path(path: Path | str | None) -> None:
    """设置当前配置路径（用于推导数据目录；None 恢复默认）。"""
    global _current_config_path
    _current_config_path = Path(path) if path is not None else None


def get_config_path() -> Path:
    """返回配置文件路径（默认 ~/.nanobot/config.json）。"""
    if _current_config_path is not None:
        return _current_config_path
    return Path.home() / ".nanobot" / "config.json"


def load_config(config_path: Path | str | None = None) -> Config:
    """加载配置：文件数据（优先）+ NANOBOT_ env（补缺）→ Config.model_validate。"""
    path = Path(config_path) if config_path is not None else get_config_path()

    data: dict[str, Any] = {}
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise ValueError(f"Failed to load config from {path}: {e}") from e
        if not isinstance(data, dict):
            raise ValueError(f"Failed to load config from {path}: root must be a JSON object")
        data = _migrate_config(data)

    merged = _merge_file_and_env(data, _env_to_config_dict())
    try:
        return Config.model_validate(merged)
    except (ValueError, pydantic.ValidationError) as e:
        raise ValueError(f"Failed to load config from {path}: {e}") from e


def save_config(config: Config, config_path: Path | str | None = None) -> None:
    """保存配置到文件（by_alias 输出；`${VAR}` 模板原样保留）。"""
    path = Path(config_path) if config_path is not None else get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = config.model_dump(mode="json", by_alias=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def merge_missing_defaults(existing: Any, defaults: Any) -> Any:
    """递归补缺省：只添加 defaults 里缺失的键，不覆盖已有值。"""
    if not isinstance(existing, dict) or not isinstance(defaults, dict):
        return existing
    merged = dict(existing)
    for key, value in defaults.items():
        if key not in merged:
            merged[key] = value
        else:
            merged[key] = merge_missing_defaults(merged[key], value)
    return merged


# ---------------------------------------------------------------------------
# NANOBOT_ env 解析（手写，替代 pydantic_settings.BaseSettings）
# ---------------------------------------------------------------------------


def _env_to_config_dict() -> dict[str, Any]:
    """把 `NANOBOT_` 前缀 env 变量解析为嵌套 dict。

    - 前缀：`NANOBOT_`；嵌套分隔符：`__`；
    - 段名一律按字段名（snake_case）小写匹配，如 `NANOBOT_AGENTS__DEFAULTS__MAX_TOKENS=100`
      → `{"agents": {"defaults": {"max_tokens": "100"}}}`（值由 pydantic 类型强转）；
    - 只收集键名非空的变量；重复段路径自动合并。
    """
    result: dict[str, Any] = {}
    for key, value in os.environ.items():
        if not key.startswith(_ENV_PREFIX):
            continue
        raw_path = key[len(_ENV_PREFIX):].strip("_")
        if not raw_path:
            continue
        parts = [part.lower() for part in raw_path.split(_ENV_NESTED_DELIMITER) if part]
        if not parts:
            continue
        cursor = result
        for part in parts[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[parts[-1]] = value
    return result


def _merge_file_and_env(file_data: dict[str, Any], env_data: dict[str, Any]) -> dict[str, Any]:
    """文件数据优先，env 只补缺失键（递归语义同 merge_missing_defaults）。"""
    return merge_missing_defaults(file_data, env_data)


# ---------------------------------------------------------------------------
# ${VAR} 环境变量引用解析
# ---------------------------------------------------------------------------


def resolve_config_env_vars(config: Config) -> Config:
    """返回 *config* 的 `${VAR}` 引用解析副本（原地递归，模板保留在文件里）。"""
    return _resolve_in_place(config)


def _resolve_in_place(obj: Any) -> Any:
    if isinstance(obj, str):
        new = _ENV_REF_PATTERN.sub(_env_replace, obj)
        return new if new != obj else obj
    if isinstance(obj, BaseModel):
        updates: dict[str, Any] = {}
        for name in type(obj).model_fields:
            old = getattr(obj, name)
            new = _resolve_in_place(old)
            if new is not old:
                updates[name] = new
        extras = obj.__pydantic_extra__
        new_extras: dict[str, Any] | None = None
        if extras:
            resolved = {k: _resolve_in_place(v) for k, v in extras.items()}
            if any(resolved[k] is not extras[k] for k in extras):
                new_extras = resolved
        if not updates and new_extras is None:
            return obj
        copy = obj.model_copy(update=updates) if updates else obj.model_copy()
        if new_extras is not None:
            copy.__pydantic_extra__ = new_extras
        return copy
    if isinstance(obj, dict):
        resolved = {k: _resolve_in_place(v) for k, v in obj.items()}
        return resolved if any(resolved[k] is not obj[k] for k in obj) else obj
    if isinstance(obj, list):
        resolved = [_resolve_in_place(v) for v in obj]
        return resolved if any(nv is not ov for nv, ov in zip(resolved, obj)) else obj
    return obj


def _env_replace(match: re.Match[str]) -> str:
    name = match.group(1)
    value = os.environ.get(name)
    if value is None:
        raise ValueError(
            f"Environment variable '{name}' referenced in config is not set"
        )
    return value


# ---------------------------------------------------------------------------
# 迁移
# ---------------------------------------------------------------------------


def _migrate_config(data: dict) -> dict:
    """旧格式 → 当前格式。

    目前只处理 nanobot 遗留的 `agents.defaults.maxMessages/max_messages`
    （replay 上限已由 loop 从 runtime 反推，legacy 字段直接丢弃）。
    """
    agents = data.get("agents", {})
    defaults = agents.get("defaults", {}) if isinstance(agents, dict) else {}
    if isinstance(defaults, dict):
        defaults.pop("maxMessages", None)
        defaults.pop("max_messages", None)
    return data
