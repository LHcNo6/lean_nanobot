# Step115 架构设计：cli_app_manager 接线（主代理 + 子代理）

## 1. 总体思路

沿用 step112 已同步的 `CliAppManager`/`CliAppsTool`，补齐「管理器实例注入」与「配置驱动注册」
两处缺失，使 `run_cli_app` 端到端可用。核心改动集中在装配层（loop / subagent / config），
工具运行时逻辑不变。

## 2. 配置建模（config/schema.py）

新增两个 pydantic 模型（与既有 `Base` 一致）：

```python
class CliAppSpec(Base):
    """单个 CLI 应用的声明。"""
    name: str
    command: str
    description: str = ""

class CliAppsConfig(Base):
    """cli_apps 配置段（对齐 nanobot ``Config.cli_apps`` 最小形态）。"""
    enable: bool = True
    apps: list[CliAppSpec] = Field(default_factory=list)
```

并在根 `Config`（`schema.py` 约 line 252）增加字段：

```python
cli_apps: CliAppsConfig = Field(default_factory=CliAppsConfig)
```

- `CliAppsTool.enabled` 已用 `getattr(config, "cli_apps", None)` 读取，`enable` 默认 True，
  新增字段后行为不变（默认启用），向后兼容。
- YAML 配置可声明：
  ```yaml
  cli_apps:
    enable: true
    apps:
      - name: lint
        command: ruff
        description: Run ruff linter
  ```

## 3. 统一构建器（tools/cli_apps.py）

新增模块级函数，集中处理「配置 → 管理器」转换，避免 loop/subagent 各自重复逻辑：

```python
def build_cli_app_manager(cfg) -> "CliAppManager":
    """从 cli_apps 配置构建 CliAppManager。"""
    mgr = CliAppManager()
    if cfg is None:
        return mgr
    for spec in getattr(cfg, "apps", None) or []:
        mgr.register(CliApp(
            name=spec.name, command=spec.command,
            description=getattr(spec, "description", "") or "",
        ))
    return mgr
```

- `cfg` 可为 `CliAppsConfig` / duck-typed / `None`；`None` 返回空管理器（兼容无配置环境）。

## 4. 注入点（两处生产 ToolContext）

### 4.1 主代理（loop.py）
- `AgentLoop.__init__`（约 line 298，紧邻 `self._exec_session_manager`）新增：
  ```python
  self._cli_app_manager = build_cli_app_manager(getattr(self.config, "cli_apps", None))
  ```
- `loop.py:1248` 的 `ToolContext(...)` 增加：
  ```python
  cli_app_manager=self._cli_app_manager,
  ```

### 4.2 子代理（subagent.py）
- `SubagentManager.__init__`（约 line 191 后）新增：
  ```python
  self._cli_app_manager = build_cli_app_manager(getattr(config, "cli_apps", None))
  ```
  （直接读原始 `config` 实参，避开 `_flatten_tools_config` 视图差异；不改签名、不改 main.py。）
- `_build_tools`（`subagent.py:216`）的 `ToolContext(...)` 增加：
  ```python
  cli_app_manager=self._cli_app_manager,
  ```

## 5. 数据流

```
YAML/Config.cli_apps.apps
   └─ build_cli_app_manager ─► CliAppManager（已注册 apps）
        ├─ loop.self._cli_app_manager ─► 主代理 ToolContext.cli_app_manager
        └─ SubagentManager.self._cli_app_manager ─► 子代理 ToolContext.cli_app_manager
              └─ CliAppsTool.create(ctx) 使用同一批已注册应用
```

- 主代理与子代理各自构建等价注册表（同一配置 → 同一批 apps）；nanobot 为共享单例，
  本 step 取等价实现，行为一致。
- `run_cli_app` 经 `create(ctx)` 读取 `ctx.cli_app_manager`，不再退化为空实例。

## 6. 利弊与风险

- 利：已同步的 `run_cli_app` 端到端可用；配置声明式注册；工具层零改动。
- 风险/注意：
  - 子代理与主代理是**两个**管理器实例（非共享单例），注册表内容一致但对象不同；
    若未来需「运行时动态增删应用并立即对所有代理生效」，需改为共享单例——本 step 不引入。
  - `CliApp.command` 在 Windows 上若含空格需自行加引号（沿用 `CliAppManager.run` 的 argv 语义）。

## 7. 不在本 step 范围

- step116：子代理 system prompt 模板化（workspace + skills_summary）；
- step117：子代理运行时限制（llm_timeout）同步；
- step118：microcompaction 工具集对齐；
- step119：self/my 工具子代理状态可观测。
