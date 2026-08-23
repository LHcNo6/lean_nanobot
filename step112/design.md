# Step112 架构设计：同步 cli_apps 与 list_exec_sessions

## 1. 总体思路

沿用 step111 已建立的「按 scope 裁剪工具集」机制（`ToolLoader.load(ctx, registry, scope=...)`），
本 step 只做**增量同步**，不改动裁剪框架本身：

- 主代理侧：loader 以 `scope="core"` 装载，`cli_apps` 与 `list_exec_sessions` 的
  `_scopes` 均含 `"core"`，因此自动出现在主代理注册表（无需手工登记）。
- 子代理侧：loader 以 `scope="subagent"` 装载，两个工具的 `_scopes` 均含 `"subagent"`，
  因此自动出现在子代理裁剪注册表，使子代理工具集从 11 → 13。

> 设计原则：对齐 nanobot「工具在自身模块里声明 `_scopes`，由 loader 按 scope 选取」，
> 而非在主代理里手工维护白名单。这也是 step111 确立的基线。

## 2. 模块改动清单

### 2.1 新增 `tools/cli_apps.py`（从 step85 同步）

- 来源：`step85/tools/cli_apps.py`，全文 `step85`→`step112` 改写 import。
- 关键修改：`CliAppsTool._scopes` 由 `{"core"}` 改为 `{"core", "subagent"}`。
- 结构（与 step85 完全一致，未改语义）：
  - `CliApp`：CLI 应用元数据（name / command / description）。
  - `CliAppManager`：内存注册表，提供 `register / get / list_names / has / run`
    （`run` 使用 `create_subprocess_exec`，argv 执行，非 shell）。
  - `CliAppsTool`：工具类，`name == "run_cli_app"`，`read_only == False`，
    `enabled()` 读 `config.cli_apps.enable`（默认 `True`），`create()` 读
    `ctx.cli_app_manager`（为 `None` 时新建空 `CliAppManager`，保证工具始终可装配）。

### 2.2 修改 `tools/exec_session.py`（从 step85 同步两个能力）

- 新增 `ExecSessionInfo` 数据类：会话摘要（`session_id / command / cwd /
  elapsed_s / status / returncode`），供 `list()` 与 `ListExecSessionsTool` 使用。
- 新增 `ExecSessionManager.list() -> list[ExecSessionInfo]`：遍历内部
  `_sessions`，根据 `process.returncode` 判定 `running`/`exited`，计算 `elapsed_s`
  （`time.monotonic() - started_at`），按 `session_id` 排序返回。
- 新增 `ListExecSessionsTool(Tool)`：`_scopes = {"core", "subagent"}`，
  `name == "list_exec_sessions"`，`read_only == True`；`enabled()` 要求
  `ctx.exec_session_manager` 非 `None`；`execute()` 调用 `manager.list()`，
  空时返回 `"No active exec sessions."`，否则按行输出 `session_id | status |
  elapsed=..s | cwd=.. | command(截断 120)`。

> 移植说明：step111 的 `ExecSessionManager` 内部 `_sessions` 结构与 step85 兼容
> （`_ExecSession` 均暴露 `session_id / command / cwd / started_at / process`），
> 因此 `list()` 可直接复用，无需改 `_ExecSession`。

### 2.3 修改 `context.py`

- `ToolContext` 新增字段 `cli_app_manager: Any = None`（默认 `None`）。
- 原因：step85 的 `CliAppsTool.create()` 依赖该字段；step112 此前缺失会导致工具
  无法装配 CLI 应用管理器，且端口的 step85 单测构造 `ToolContext` 时会抛
  `TypeError`。新增字段属低成本必需项，且对既有装配（未传该字段）向后兼容。

### 2.4 测试改动

- `tests/test_subagent_tool_isolation.py`：`SUBAGENT_TOOL_NAMES` 由 11 增至 13
  （新增 `run_cli_app`、`list_exec_sessions`），契约 B1 断言文案同步更新。
- 端口 `step85/tests/test_cli_apps.py`、`step85/tests/test_list_exec_sessions.py`
  到 `step112/tests/`（import 路径改写 `step85`→`step112`）。

## 3. 数据流与装配时序

```
loop / SubagentManager
   │  构造 ToolContext(config, workspace, ..., exec_session_manager, cli_app_manager)
   ▼
ToolLoader.load(ctx, registry, scope="subagent")
   │  遍历 tools 包，仅保留 _scopes 含 "subagent" 且 enabled(ctx) 为真的工具
   ▼
子代理注册表 = { exec, read_file, ..., write_stdin, apply_patch,
                 run_cli_app, list_exec_sessions }   # 共 13
```

- `run_cli_app`：`enabled` 读 `config.cli_apps.enable`（缺省 `True`）→ 入选；
  `create` 取 `ctx.cli_app_manager`（子代理场景下为 `None` → 空 `CliAppManager`，
  工具可见但无注册应用，符合最小同步目标）。
- `list_exec_sessions`：`enabled` 要求 `exec_session_manager`（子代理场景由
  `SubagentManager` 注入共享的 `ExecSessionManager`）→ 入选并可读真实会话。

## 4. 利弊与风险

- 利：与 step85 实现 100% 一致；最小增量；子代理能力与 nanobot 对齐。
- 弊/风险：
  - `cli_app_manager` 在 step112 主代理的 loop 中尚未被接线注册具体应用，
    `run_cli_app` 默认只能用空白名单（执行任何名字都会 `Unknown CLI app`）。
    这是「同步工具」与「接线 CLI 应用注册」两个关注点的边界，后者留作后续 step。
  - 子代理场景下 `cli_app_manager` 为 `None`（空管理器），子代理本身也无法注册
    新应用（安全上合理：子代理不应拥有主代理的 CLI 应用白名单）。

## 5. 不在本 step 范围

- 不在 `main.py` / `loop.py` 中接线 `CliAppManager`（如从配置注册 CLI 应用）。
- 不改造 `list_exec_sessions` 的输出格式或新增过滤参数。
- 不处理 nanobot 的 `owner_session_key` 会话隔离等高级特性。
