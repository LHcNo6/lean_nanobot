# Step111 接口契约：Subagent 工具集隔离

## 1. 公开接口变更

### 1.1 SubagentManager（step111/subagent.py）

```python
class SubagentManager:
    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider | None = None,
        *,
        config: Any = None,                    # 完整 Config / 扁平 duck-view / None
        workspace: str = "",                   # 子代理工具工作区根
        restrict_to_workspace: bool | None = None,  # None → 回落 config.tools.*
        max_concurrent_subagents: int = 5,
        max_iterations: int = 10,
    ) -> None: ...

    def _build_tools(self) -> ToolRegistry:
        """构建子代理专用工具注册表；每次调用返回全新实例。"""
```

**破坏性变更**：移除 `tools: ToolRegistry | None = None` 参数。
所有旧式 `SubagentManager(bus=..., tools=registry)` 调用点必须迁移。

### 1.2 装配契约（step111/main.py）

```python
subagent_manager = SubagentManager(
    bus=bus,
    provider=snapshot.provider,
    config=config,                 # 完整 Config，内部扁平化
    workspace=str(config.workspace_path),
    max_concurrent_subagents=defaults.max_concurrent_subagents,
)
```

### 1.3 行为契约

| # | 契约 |
|---|------|
| B1 | `_build_tools()` 返回的 registry 恰含 11 个名字：`exec`、`read_file`、`write_file`、`edit_file`、`list_dir`、`find_files`、`grep`、`web_search`、`web_fetch`、`write_stdin`、`apply_patch`（以 `config` 各组 enable=True 为前提） |
| B2 | registry 不含：`spawn`、`message`、`create_goal`、`update_goal`、`my`/`self` 组、`cron_*`、`mcp_*`、`echo`、`generate_image`、`glob` |

> 注：nanobot 子代理工具集为 13 个（多出 `cli_apps` 与 `list_exec_sessions`，
> 二者在 learn_nano 中尚未实现对应工具类），本 step 以 learn_nano 现有
> 已声明 subagent scope 的全集为准。
| B3 | 子代理运行时 `AgentRunSpec.tools == _build_tools()` 结果；递归 spawn 表现为"未知工具"错误结果，而非真实生成新任务 |
| B4 | 主 agent 的 registry 内容与 spawn 工具行为不受本变更影响 |
| B5 | `restrict_to_workspace=True` 时文件类工具 `allowed_dir=workspace`；False/未传且 config 未声明时无目录限制 |
| B6 | 连续两次 `_build_tools()` 返回不同 `file_state_store` 实例（互不共享 read-dedup 状态）；`exec_session_manager` 同一实例 |
| B7 | `config=None` 时按默认 `ToolsConfig()` 构造视图，B1 全集仍可装载 |
| B8 | spawn 并发闸门、task_id/display_label 规则、announce 回注、cancel_by_session 语义与 step110 完全一致 |

### 1.4 配置扁平化契约

模块级私有 `_flatten_tools_config(config: Any) -> Any`：

- 输入完整 `Config`（有 `.tools`、无根级 `.web`）→ 输出
  `SimpleNamespace(web, exec, tools)` 三段视图；
- 输入已扁平对象（同时有根级 `.web` 与 `.tools`）→ 原样返回；
- 输入 None → 从空 `ToolsConfig()` 构造同构视图；
- `restrict_to_workspace` 显式实参优先于配置值。

## 2. 测试契约

新增 `step111/tests/test_subagent_tool_isolation.py`，全部 mock，禁止真实网络/API：

| 用例 | 验证契约 |
|------|----------|
| test_build_tools_contains_exactly_subagent_set | B1 |
| test_build_tools_excludes_core_only_tools | B2 |
| test_spawned_subagent_cannot_recursive_spawn | B3（mock provider 返回 spawn 调用 → 断言工具执行结果为未知工具错误、manager 无新任务产生） |
| test_group_toggle_respected | 关闭 web 组后 web_search/web_fetch 缺席（组级开关生效） |
| test_file_state_isolated_per_spawn | B6 |
| test_flatten_config_forms | 扁平化三形态 + restrict 显式覆盖 |

回归范围：`test.py -k "Subagent or injection"`、`tests/test_max_iterations.py`、
`tests/` 全量。

## 3. 兼容性说明

- `AgentRunSpec` / `AgentRunner` / loop / announce 协议零改动；
- 既有测试构造 `SubagentManager(bus=..., max_concurrent_subagents=...)` 不传 tools，
  天然兼容新签名；
- step111 文件夹为独立快照，包内 import 已整体替换为 `step111.*`。
