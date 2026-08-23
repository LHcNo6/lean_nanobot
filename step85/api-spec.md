# Step 85 API Specification

## 1. ToolLoader（无变更）

**文件**：`loader.py`

### discover()

```python
def discover(self) -> list[type[Tool]]
```

自动发现 tools 目录下所有 Tool 子类。

跳过模块：`_SKIP_MODULES` 中的模块（base, schema, registry, context, loader, config, file_state, sandbox, mcp, __init__）。

发现条件：
- 是 Tool 的子类
- 不是 Tool 本身
- 类名不以 "_" 开头
- 没有抽象方法
- `_plugin_discoverable` 为 True（默认 True）

### load()

```python
def load(self, ctx, registry, *, scope="core") -> list[str]
```

发现工具 → 检查 scope → 检查 enabled → create → register。

## 2. 新增工具类确认

| 工具类 | 所在文件 | 名称 | config_key |
|--------|----------|------|------------|
| CliAppsTool | cli_apps.py | run_cli_app | cli_apps |
| ListExecSessionsTool | exec_session.py | list_exec_sessions | exec |

## 3. 测试 API

**文件**：`tests/test_tool_discovery.py`

测试用例：
- test_discover_returns_non_empty
- test_discover_contains_cli_apps_tool
- test_discover_contains_list_exec_sessions_tool
- test_discover_sorted_by_name
- test_discover_excludes_abstract
- test_load_registers_tools
- test_discover_count_matches_expected
