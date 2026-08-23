# Step115 接口契约（api-spec）

本文件定义 step115「cli_app_manager 接线」的对外契约，供实现与测试对齐。

## C1：配置 Schema（config/schema.py）

新增两个模型并挂到根 `Config`：

```python
class CliAppSpec(Base):
    name: str
    command: str
    description: str = ""

class CliAppsConfig(Base):
    enable: bool = True
    apps: list[CliAppSpec] = Field(default_factory=list)

# Config 增加字段：
cli_apps: CliAppsConfig = Field(default_factory=CliAppsConfig)
```

- `CliAppsTool.enabled(ctx)` 经 `getattr(ctx.config, "cli_apps", None)` 读取；
  新增字段后默认 `enable=True`，与既有行为一致。
- 配置加载（pydantic）自动校验 `cli_apps.apps` 中每项含 `name`/`command`。

## C2：统一构建器（tools/cli_apps.py）

```python
def build_cli_app_manager(cfg: Any | None) -> "CliAppManager": ...
```

契约：
- `cfg is None` → 返回空 `CliAppManager()`。
- `cfg` 含 `apps`（list，每项有 `name`/`command`/`description?`）→ 逐个 `mgr.register(CliApp(...))`。
- 返回 `CliAppManager` 实例（已注册）。

## C3：注入契约（loop.py / subagent.py）

- `AgentLoop.__init__` 产生 `self._cli_app_manager = build_cli_app_manager(getattr(self.config, "cli_apps", None))`；
  主代理 `ToolContext(cli_app_manager=self._cli_app_manager, ...)`。
- `SubagentManager.__init__` 产生 `self._cli_app_manager = build_cli_app_manager(getattr(config, "cli_apps", None))`；
  子代理 `_build_tools` 的 `ToolContext(cli_app_manager=self._cli_app_manager, ...)`。

## C4：工具层接口不变

`CliAppsTool` 的 `name` / 参数 / 行为不变；仅 `create(ctx)` 现在拿到真实 `cli_app_manager`
（不再退化为空实例）。`run_cli_app` 对**已注册**应用执行成功，对未知应用返回
`Error: Unknown CLI app '<name>'. Available: ...`。

## C5：测试映射

| 契约 | 测试 |
| --- | --- |
| C2 | `build_cli_app_manager`：有 apps→注册；None→空 |
| C2+C4 | `CliAppsTool.create(ToolContext(cli_app_manager=mgr)).execute(...)` 实际跑通 app |
| C3 | `SubagentManager(config=含apps)._build_tools()` 中 `run_cli_app` 的 `_manager.list_names()` 含该 app |
| C3 | 主代理 `loop` 注入（通过共享构建器逻辑保证，等同子代理接线） |

> 全部测试使用 mock / 构造数据，禁止真实网络与 API 调用。
