# Step112 需求定义：同步 cli_apps 与 list_exec_sessions 工具

## 1. 问题背景

在 step111 中，我们为子代理（subagent）构建了独立的「scope=subagent」工具集，使其与
nanobot 的 `_build_tools` 对齐。但经核对发现：

- **nanobot** 的子代理工具集共 **13** 个，多出的两个是 `cli_apps`（对应工具名
  `run_cli_app`）与 `list_exec_sessions`。
- **step111** 的子代理工具集只有 **11** 个，缺少上述两个。

进一步追溯原因：这两个工具其实在 **step83 / step84 / step85** 中已经实现
（`step85/tools/cli_apps.py`、`step85/tools/exec_session.py` 中的 `ListExecSessionsTool`
以及 `ExecSessionManager.list()`），但 step110/step111 这条分支线在 fork 时并未包含它们
（step111 的 `exec_session.py` 仅移植了 `WriteStdinTool`，缺少 `list_exec_sessions` 相关实现；
并且 step111 目录中根本没有 `cli_apps.py`）。

> 用户明确指出：这两个工具在 step83-85 已实现，只是没同步过来，要求在 step112 中修复。

## 2. 本 step 要解决什么

将 `cli_apps`（`run_cli_app`）与 `list_exec_sessions` 两个工具**同步**进 step112，使子代理
工具集从 11 个补齐到 **13 个**，与 nanobot 完全对齐。同时保证这两个工具在主代理（scope=core）
侧也可用。

## 3. 为什么这样做（方案取舍）

- **方案 A：在 step112 重新实现** —— 重复造轮子，且易与 step85 既有实现产生不一致，
  违背「对齐、可追溯」目标。**否决**。
- **方案 B（选定）：从 step85 同步源码** —— 仅做 `step85`→`step112` 的 import 路径改写，
  并对 `cli_apps` 的 `_scopes` 追加 `"subagent"`（step85 当年只声明了 `"core"`，因为当时
  还没有子代理 scope 概念）。优点：实现与 step85 完全一致、最小增量、可追溯；代价是需确认
  step112 的依赖是否齐备。

### 同步过程中暴露的依赖缺口（必须一并补齐）

1. `step112/context.py` 的 `ToolContext` **缺少 `cli_app_manager` 字段**。
   `CliAppsTool.create()` 通过 `getattr(ctx, "cli_app_manager", None)` 读取该字段，
   step85 当年正是因为有了这个字段才可用；step112 必须补上，否则该工具在所有真实装配
   场景下都只能用空管理器（且端口的 step85 单测会直接因构造 `ToolContext(..., cli_app_manager=...)`
   报错）。
2. `step112/tools/exec_session.py` 只有 `WriteStdinTool`，**缺少 `ExecSessionManager.list()`
   与 `ListExecSessionsTool`**。不补齐则 `list_exec_sessions` 工具无法工作（无数据来源）。

上述缺口属于「让被同步工具真正可用」的必改项，纳入本 step 一并处理（仍是最小增量）。

## 4. 目标与实现边界（最小增量）

- 目标：子代理工具集 = 13 个，与 nanobot 对齐。
- 边界：**不**重新设计工具语义、**不**改动 step85 已稳定的工具内部逻辑、**不**在 main.py
  中额外接线 `cli_app_manager`（工具默认回退空管理器即可；接线注册 CLI 应用留作后续 step）。
- 唯一新增的字段改动是 `ToolContext.cli_app_manager`（为让工具可被装配），属低成本必需项。

## 5. 验收标准

1. `SubagentManager._build_tools()` 返回的子代理注册表**恰含 13 个**工具，不多不少，
   且含 `run_cli_app` 与 `list_exec_sessions`。
2. 核心专属工具（spawn / message / create_goal / update_goal / echo / generate_image /
   glob）在子代理注册表中不可见。
3. `CliAppsTool` 与 `ListExecSessionsTool` 在主代理（scope=core）侧可被加载。
4. 两个工具的单元测试从 step85 端口到 step112 并全部通过（使用 mock / 构造数据，
   禁止真实网络与 API 调用）。
5. step112 全量测试相对 step111 基线无新增回归。
