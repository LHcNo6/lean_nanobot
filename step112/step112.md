# Step112 配套文档：同步 cli_apps 与 list_exec_sessions 工具

## 1. 问题背景

step111 为子代理（subagent）建立了「按 scope 裁剪」的独立工具集，与 nanobot 的
`_build_tools` 机制对齐。但核对 nanobot 发现：

- **nanobot** 子代理工具集共 **13** 个，包括 `cli_apps`（`run_cli_app`）与 `list_exec_sessions`；
- **step111** 只有 **11** 个，缺这两个。

追溯根因：这两个工具其实在 **step83 / step84 / step85** 已实现
（`step85/tools/cli_apps.py`、以及 `step85/tools/exec_session.py` 的 `ListExecSessionsTool`
和 `ExecSessionManager.list()`），但 step110/step111 这条分支线在 fork 时并未携带它们
（step111 的 `exec_session.py` 仅移植了 `WriteStdinTool`，`cli_apps.py` 更是整个缺失）。

> 用户明确指出：这两个工具在 step83-85 已实现、只是没同步过来，要求在 step112 中修复。

## 2. 本 step 解决什么 & 为什么这样做

**目标**：把 `run_cli_app` 与 `list_exec_sessions` 同步进 step112，使子代理工具集从 11 → 13，
与 nanobot 完全对齐；同时保证主代理（scope=core）侧也能用这两个工具。

**为什么**（方案取舍）：

- 方案 A「在 step112 重写」：重复造轮子，易与 step85 不一致 → **否决**。
- 方案 B「从 step85 同步源码，仅改写 import 路径」：实现与 step85 100% 一致、最小增量、
  可追溯 → **选定**。

同步中暴露两个依赖缺口，一并补齐（属「让被同步工具真正可用」的必改项）：

1. `step112/context.py` 的 `ToolContext` 此前**没有 `cli_app_manager` 字段**，
   `CliAppsTool.create()` 依赖它；补上后端口的 step85 单测才能通过。
2. `step112/tools/exec_session.py` 只有 `WriteStdinTool`，**缺 `ExecSessionManager.list()`
   与 `ListExecSessionsTool`**；从 step85 移植这两个能力（`_ExecSession` 内部结构兼容，可直接复用）。

## 3. 原理思路与具体实现

沿用 step111 的「工具在自身模块声明 `_scopes`，由 loader 按 scope 选取」机制，本 step 不改动
裁剪框架，只做增量同步：

- **新增 `tools/cli_apps.py`**（从 step85 全文同步，`step85`→`step112` 改写 import）：
  - `CliApp` / `CliAppManager` / `CliAppsTool` 与 step85 完全一致；
  - 唯一改动：`CliAppsTool._scopes` 由 `{"core"}` 改为 `{"core", "subagent"}`，
    使其进入子代理工具集。
- **修改 `tools/exec_session.py`**（从 step85 同步两个能力）：
  - 新增 `ExecSessionInfo` 数据类（会话摘要）；
  - 新增 `ExecSessionManager.list() -> list[ExecSessionInfo]`（按 `session_id` 排序、
    据 `process.returncode` 判定 `running`/`exited`）；
  - 新增 `ListExecSessionsTool`（`_scopes={"core","subagent"}`，`read_only=True`，
    `enabled` 需 `exec_session_manager`，`execute` 调用 `manager.list()` 文本化输出）。
- **修改 `context.py`**：`ToolContext` 新增 `cli_app_manager: Any = None` 字段（缺省 `None`，
  向后兼容）。
- **测试**：
  - `tests/test_subagent_tool_isolation.py`：`SUBAGENT_TOOL_NAMES` 由 11 → 13，B1 断言同步；
  - 端口 `step85/tests/test_cli_apps.py`、`test_list_exec_sessions.py` 到 `step112/tests/`。

## 4. 核心函数 / 类功能说明

| 符号 | 位置 | 功能 |
| --- | --- | --- |
| `CliAppsTool` | tools/cli_apps.py | 执行已注册 CLI 应用（argv 子进程，非 shell），`name="run_cli_app"` |
| `CliAppManager.run` | tools/cli_apps.py | 用 `create_subprocess_exec` 运行白名单应用，合并 stdout/stderr，含超时处理 |
| `ExecSessionManager.list` | tools/exec_session.py | 返回当前全部执行会话的 `ExecSessionInfo` 摘要列表（按 `session_id` 排序） |
| `ListExecSessionsTool` | tools/exec_session.py | 列出活跃 exec 会话，`name="list_exec_sessions"`，只读 |
| `ToolContext.cli_app_manager` | context.py | 新增字段，供 `CliAppsTool.create` 注入 CLI 应用管理器 |

## 5. 验证结果

- 子代理注册表（`scope="subagent"`）恰含 **13** 个工具，含 `run_cli_app` / `list_exec_sessions`
  （`test_subagent_tool_isolation.py` 通过）。
- 主代理注册表（`scope="core"`）实测包含 `run_cli_app` 与 `list_exec_sessions`（冒烟测试 19 个工具）。
- 端口的 `test_cli_apps.py` + `test_list_exec_sessions.py` 全部通过（mock/构造数据，无真实网络）。
- step112 全量 `tests`：**25 failed / 1135 passed**，与 step111 基线（25 failed / 1093 passed）
  相比**无新增回归**，新增通过的 42 例来自本 step 端口的两个测试文件。

## 6. 暴露了什么问题

1. **CLI 应用白名单尚未接线**：step112 的 `ToolContext.cli_app_manager` 默认 `None`，
   `run_cli_app` 只会回退空 `CliAppManager`——执行任何名字都报 `Unknown CLI app`。
   要真正可用，需要在 `main.py`/`loop.py` 中创建 `CliAppManager` 并从配置注册应用，再注入
   `ToolContext.cli_app_manager`。这是「同步工具」与「接线应用注册」的边界，本 step 未做。
2. **子代理无 CLI 应用白名单**：子代理场景下 `cli_app_manager` 为 `None`（空管理器），
   子代理无法注册/调用主代理的 CLI 应用——安全上合理，但意味着子代理的 `run_cli_app` 实际不可用。
3. **fork 流程教训**：本次最初用 PowerShell `Set-Content` 做 `step111→step112` 全量替换时，
   因默认编码（GBK/ANSI）**破坏了 UTF-8 中文注释**，后改用 Python（`utf-8`）重做 fork 才修复。
   后续 fork 必须用 UTF-8 安全的复制/替换方式。

## 7. 下一 step 要解决什么

- 在 `main.py` / `loop.py` 中接线 `CliAppManager`：从配置（如 `config.cli_apps.apps`）注册
  CLI 应用白名单，并注入 `ToolContext.cli_app_manager`，使主代理的 `run_cli_app` 真正可用。
- （可选）评估是否让子代理以只读/受限方式共享部分 CLI 应用；若不需要，明确在文档中标注
  子代理 `run_cli_app` 不可用，避免误导。
- 对齐 nanobot 的 `list_exec_sessions` 高级特性（如 `owner_session_key` 会话隔离、按状态过滤）。
