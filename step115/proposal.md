# Step115 需求定义：cli_app_manager 接线（主代理 + 子代理，含配置 Schema）

## 1. 问题背景

step114 已完成执行会话 `owner_session_key` 隔离。但 step112 同步的 `run_cli_app`
（CLI 应用白名单工具）至今**不可用**：`CliAppsTool.create(ctx)` 在 `ctx.cli_app_manager`
为 `None` 时退化为一个**空** `CliAppManager`（`cli_apps.py:181-184`）。`ToolContext.cli_app_manager`
字段（`context.py:111`）虽已定义，但生产代码（主代理 `loop.py:1248`、子代理 `subagent.py:216`）
从不注入真实实例，因此 `run_cli_app` 对任何名字都报 "Unknown CLI app"。

此外，配置中完全没有 `cli_apps` 段，无法声明要注册的 CLI 应用。

## 2. 本 step 要解决什么

- 让 `run_cli_app` 真正可用：在主代理与子代理的 `ToolContext` 中注入真实 `CliAppManager`。
- 支持配置驱动的应用注册：新增 `cli_apps.apps` 配置段，启动时自动注册 CLI 应用。

## 3. 为什么这样做（方案取舍）

- 方案 A「只在 loop/subagent 注入一个空 manager」：范围小，但 `run_cli_app` 仍无应用可用，
  端到端不可用。与「让已同步工具生效」的目标不符。**否决**。
- 方案 B（选定）：新增 `CliAppsConfig`/`CliAppSpec` 配置 Schema + 统一构建器
  `build_cli_app_manager`，在 loop 与 subagent 处从 `config.cli_apps.apps` 注册应用并注入
  `ToolContext`。对齐 nanobot 的 `cli_app_manager` 接线理念；工具层零改动。

## 4. 目标与实现边界（最小增量）

- 目标：主/子代理的 `run_cli_app` 绑定真实 `CliAppManager`；配置含 apps 时可直接执行。
- 边界（**不做**）：
  - 不实现 nanobot 的 catalog 缓存 / 运行时 context / 应用安装卸载；
  - 不实现「跨进程共享单例 manager」（主代理与子代理各自按同一配置构建等价注册表，行为等价）；
  - 不改动 `CliAppManager`/`CliAppsTool` 的运行时语义。

## 5. 验收标准

1. `cli_apps.py` 新增 `build_cli_app_manager(cfg)`：cfg=None 返回空管理器；cfg 含 apps 则注册。
2. `config/schema.py` 新增 `CliAppSpec`/`CliAppsConfig` 并挂到 `Config.cli_apps`。
3. `loop.py` 构造 `self._cli_app_manager` 并注入主代理 `ToolContext`。
4. `subagent.py` 构造 `self._cli_app_manager` 并注入子代理 `ToolContext`。
5. 测试：构建器单测 + 实际跑通一个 app + 子代理 `_build_tools` 接线验证。
6. 全量测试失败数与 step114 基线（25）持平，无新增回归。
