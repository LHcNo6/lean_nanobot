# Step115：cli_app_manager 接线（主代理 + 子代理，含配置 Schema）

## 1. 问题背景

step112 已把 `run_cli_app`（CLI 应用白名单工具）同步进 step114，但一直**不可用**：
`CliAppsTool.create(ctx)` 在 `ctx.cli_app_manager` 为 `None` 时退化为一个**空**
`CliAppManager`（`cli_apps.py:181-184`）；`ToolContext.cli_app_manager` 字段（`context.py:111`）
虽已定义，主代理（`loop.py:1248`）与子代理（`subagent.py:216`）的 `ToolContext` 从不注入真实实例，
故 `run_cli_app` 对任何名字都报 "Unknown CLI app"。此外配置中完全没有 `cli_apps` 段，无法声明应用。

## 2. 本 step 解决了什么 / 为什么这样做

- **解决**：让 `run_cli_app` 端到端可用——在主/子代理 `ToolContext` 注入真实 `CliAppManager`，
  并支持 `cli_apps.apps` 配置驱动的应用注册。
- **为什么**：集中式「配置 → 管理器」构建器 `build_cli_app_manager`，在 loop 与 subagent 两处复用，
  工具层零改动；对齐 nanobot 的 `cli_app_manager` 接线理念（命名化白名单应用）。
- **利弊**：利——已同步工具生效、配置声明式注册；弊——主/子代理各自按同一配置构建等价注册表
  （非 nanobot 的共享单例，对象不同但内容一致），动态增删应用需后续改为共享单例。

## 3. 核心实现

- `config/schema.py`：新增 `CliAppSpec(name/command/description)` 与 `CliAppsConfig(enable/apps)`，
  并在根 `Config` 增加 `cli_apps` 字段（默认启用，向后兼容）。
- `tools/cli_apps.py`：新增 `build_cli_app_manager(cfg)`——`cfg=None` 返回空管理器，否则遍历
  `cfg.apps` 注册为 `CliApp`。
- `loop.py`：`__init__` 生成 `self._cli_app_manager = build_cli_app_manager(getattr(self.config, "cli_apps", None))`，
  主代理 `ToolContext` 注入 `cli_app_manager=self._cli_app_manager`。
- `subagent.py`：`__init__` 生成 `self._cli_app_manager = build_cli_app_manager(getattr(config, "cli_apps", None))`，
  子代理 `_build_tools` 的 `ToolContext` 注入 `cli_app_manager=self._cli_app_manager`。
- 工具层零改动。

## 4. 核心函数 / 类说明

- `build_cli_app_manager(cfg) -> CliAppManager`：配置到管理器的唯一转换点（loop/subagent 共用）。
- `CliAppsConfig`：配置段，YAML 例：
  ```yaml
  cli_apps:
    enable: true
    apps:
      - name: lint
        command: ruff
        description: Run ruff linter
  ```
- `SubagentManager._cli_app_manager` / `AgentLoop._cli_app_manager`：各自按配置构建的等价注册表。

## 5. 测试

- `tests/test_cli_apps.py::TestBuildCliAppManager`：`build_cli_app_manager` 单测（None→空；specs→注册；schema 配置→注册）。
- `tests/test_cli_apps.py::TestCliAppsToolRealExecution`：注入真实 manager 后 `run_cli_app` 真实跑通一个
  `sys.executable -c "print('hello-cli')"` 应用。
- `tests/test_subagent_tool_isolation.py::TestSubagentCliAppManagerWiring`：子代理 `_build_tools` 注入的
  `run_cli_app` 工具管理器含配置中的应用；无配置时为空（向后兼容）。
- 全量 `step115/tests`：**25 failed / 1147 passed**（失败数与 step114 基线持平，通过 +6 即本 step 新增用例）。

## 6. 暴露的问题

- 主代理与子代理是两个独立 `CliAppManager` 实例（非共享单例）。当前注册表内容一致，但若未来需要
  「运行时动态增删应用对所有代理即时生效」，需改为共享单例——本 step 未引入。
- `CliApp.command` 在 Windows 上若含空格需自行加引号（`CliAppManager.run` 走 argv 语义）。

## 7. 下一步（step116）

step116：子代理 system prompt 模板化——抽离硬编码 prompt 为模板，渲染 `workspace` + `skills_summary`
（接入 `SkillsLoader`），对齐 nanobot `subagent_system.md`。
