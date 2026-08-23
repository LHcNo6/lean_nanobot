# 子代理（subagent）对齐 nanobot 路线图（step110 → step119）

> 目的：以最小增量原则，逐步把 learn_nano 的子代理（subagent）核心对齐到 nanobot，
> 每一步单文件夹、单特性、可独立运行，并提交。
>
> 规则（AGENTS.md）：原理先行 → 先写 proposal/design/api-spec 三份规范 → 实现 →
> 配套 `.md`（问题背景/原理/函数/暴露问题/下一步）→ 跑通测试 → 代码审查 → 提交并推送。
> fork 必须用 Python（utf-8 安全），禁用 PowerShell `Set-Content`（破坏中文）。
> 测试禁止真实 API Key / 网络，一律 mock。

---

## 0. 对齐现状（已完成）

| Step | 内容 | commit | 备注 |
| --- | --- | --- | --- |
| step110 | subagent vs nanobot 对齐度分析（架构/通信/运行时/持久化/取消） | `c143f12` | 问题地图 |
| step111 | 子代理工具集隔离（scope=`subagent` 裁剪版 registry + 防递归双保险） | `d624f19` | 11 工具 |
| step112 | 同步 cli_apps（`run_cli_app`）+ list_exec_sessions → 工具集 13 个对齐 | `5fef686` | 新增 2 工具 |
| step113 | 子代理 RequestContext + workspace_scope 绑定（session_key 注入运行上下文） | `41272d5` | 运行上下文对齐 |

**全量测试基线（step113）**：25 failed / 1137 passed（失败均为 Windows 环境既有问题，
如 `env={}` 触发 `WinError 87`、proactor 子进程 teardown 告警，与 subagent 逻辑无关）。

---

## 1. 已完成：step114 — exec_session owner_session_key 隔离

- **目标**：为长运行执行会话引入 `owner_session_key` 归属与按 owner 过滤，子代理会话归属父会话、跨会话不可互见。
- **实现**（`tools/exec_session.py`）：`_ExecSession` 加 `owner_session_key` 字段；`start()` 创建后打标
  `current_request_session_key()`（复用 step113 注入的 session_key）；`get`/`write`/`list` 集中式按 owner
  过滤（owner=None 或观察者无上下文则全员可见；否则仅 owner==当前会话可见）。工具层零改动。
- **测试**：新增 4 例（3 manager 级 + 1 `list_exec_sessions` 工具级）全绿。
- **结果**：全量 `step114/tests` = 25 failed / 1141 passed（失败数持平，通过 +4 即本 step 新增）。
- **commit**：`a425562`（已推送 `41272d5..a425562 main -> main`）。

---

## 2. 后续规划（step115 → step119）

> 每个 step 仅做最小增量；下列范围为建议，实现时以三份规范为准。

### step115：cli_app_manager 接线（主代理 + 子代理）
- **目标**：使 step112 已同步的 `run_cli_app` 真正可用（当前 `ToolContext.cli_app_manager` 已存在但未注入实例）。
- **最小范围**：
  - `main.py` / `loop.py`：从 config（如 `cli_apps.apps`，缺省空）构造 `CliAppManager` 并注入 `ToolContext.cli_app_manager`。
  - `subagent.py` `_build_tools`：注入 `cli_app_manager=self._cli_app_manager`（管理器共享一个，缺省空；可按配置加载）。
- **对齐 nanobot**：`tool_context.py` 的 `cli_app_manager` 接线；子代理继承父代理的应用目录。
- **文件**：`main.py`/`loop.py`、`subagent.py`、`SubagentManager`。
- **测试**：主/子代理 `run_cli_app` 在无配置下可用（返回空/提示）；注入后可用 mock app。

### step116：子代理 system prompt 模板化
- **目标**：抽离 step113 硬编码的 `_SUBAGENT_SYSTEM_PROMPT`，改为模板渲染 `workspace` + `skills_summary`，
  对齐 nanobot `subagent_system.md`。
- **最小范围**：
  - 新增模板（如 `templates/agent/subagent_system.md`），由 `workspace` 与 `skills_summary` 渲染。
  - 在 `_run_subagent` 接入 `SkillsLoader.build_skills_summary`（尊重子代理禁用 skills），注入 prompt。
- **文件**：`subagent.py`、`templates/...`、可选 `skills/loader.py`。
- **测试**：子代理 prompt 含 workspace 路径与 skills 摘要；禁用 skills 不出现。

### step117：子代理运行时限制同步
- **目标**：把父会话的运行时限制同步到子代理（墙钟超时等），对齐 nanobot `_sync_subagent_runtime_limits`。
- **最小范围**：
  - 扩展同步逻辑，将 `llm_timeout_s`（及 model/runtime）写入 `AgentRunSpec.llm_timeout_s`。
  - 子代理运行受父策略约束。
- **文件**：`loop.py`、`subagent.py`（`AgentRunSpec` 已有 `llm_timeout_s` 字段则直接赋值）。
- **测试**：子代理 agent 的 `llm_timeout_s` 等于父会话配置；mock provider 验证超时生效。

### step118：子代理 microcompaction 工具集对齐（校验）
- **目标**：确认/补全流程压缩（microcompaction）覆盖子代理相关工具，确保 `list_exec_sessions` 在可压缩集合内。
- **最小范围**：
  - 核查 `governance.py`（`ContextGovernor`/截断逻辑）的工具集；若 `list_exec_sessions` 缺失则补入。
  - 若 step113 的 governance 已覆盖则本 step 退化为零改动校验。
- **文件**：`governance.py`（step113 实际路径为 `stepXXX/governance.py`，非 `context_governance.py`）。
- **测试**：压缩触发时 `list_exec_sessions` 摘要被保留/压缩符合预期。

### step119（可选）：self/my 工具可观测子代理状态
- **目标**：让 `self`/`my` 工具读取 `SubagentManager._task_statuses`，父代理可查询运行中子代理，对齐 nanobot `self.py`。
- **最小范围**：`self`/`my` 工具接入 `subagent_manager`，输出当前子代理任务状态（running/done/error）。
- **文件**：`tools/self.py`（或 `my.py`）、`subagent.py`。
- **测试**：启动子代理后，`self` 工具输出含该子代理状态。

---

## 3. 完成 115–119 后的对齐度

| 维度 | 对齐项 | 覆盖 step |
| --- | --- | --- |
| 编排 | 后台 AgentRunner、结果回注总线、`/stop` 取消 | 111/113 |
| 工具集 | scope=subagent 13 工具、防递归 | 111/112 |
| 运行上下文 | RequestContext + workspace_scope 绑定 | 113 |
| 会话隔离 | exec_session owner_session_key | 114 |
| 应用目录 | cli_app_manager 接线 | 115 |
| prompt | 模板化（workspace + skills_summary） | 116 |
| 运行时限制 | llm_timeout 等同步 | 117 |
| 流程压缩 | 工具集对齐（含 list_exec_sessions） | 118 |
| 可观测 | self/my 子代理状态 | 119 |

**核心对齐达成**：子代理可并行、隔离、取消、受限运行，会话与工具均按 owner 隔离，prompt 携带 workspace 与 skills 上下文；退出自动取消（`cancel_by_session`）与 mid-turn announce 注入（`session_key_override`）**均已实现**（step119 调研确认，比路线图预期更完整）。
剩余高级打磨：announce 模板渲染 `subagent_announce.md` 与 body 通道清洗 `subagent_channel_display`、runtime 逐父同步等（见 §6）。

---

## 4. 实现注意事项（踩坑记录）

- **fork 中文编码**：用 Python `shutil.copytree` + `read/write(encoding="utf-8")` 落地；PowerShell `Set-Content` 默认 GBK 会破坏中文注释（step112 已踩过）。
- **测试事件循环**：`ExecSessionManager.start` 与 `write`/`terminate` 必须在**同一** `asyncio.run` 内——
  子进程 transport 绑定创建它的事件循环，跨循环调用会 `RuntimeError: Event loop is closed`。
- **Windows 子进程**：测试传入 `env={}` 会触发 `WinError 87`；应传 `env=None`（继承父环境）。这是 step113 既有
  `test_start_and_poll`/`test_session_removed_after_done` 失败的根因，与本 step 逻辑无关。
- **pytest Windows 告警**：`unraisableexception` 插件在子进程 teardown 时会把 proactor 告警升级，可用
  `-p no:unraisableexception` 规避内部崩溃（不影响断言结果）。
- **import 路径**：测试须从仓库根目录运行（`python -m pytest stepXXX/tests ...`），否则 `stepXXX` 包不可导入。

---

## 5. 下一步建议（更新于 step119 完成后）

step110–119 核心九维度已全部对齐，且 `cancel_by_session` 与 mid-turn announce 注入也确认已实现。
下一步推进 **step120：子代理运行配置传播（G1–G4）**——把 `config` 的 `max_tool_result_chars` /
`fail_on_tool_error` / `finalize_on_max_iterations` / 收尾与错误文案 透传到子代理 `AgentRunSpec`，
这是唯一影响子代理**实际运行行为正确性**的缺口，优先级最高（见 §6 规划）。

---

## 6. 后续规划（step120+）：剩余未对齐项与最小增量拆解

> step119 完成后的差距对比如下。所有项均属「增强 / 打磨」层；核心能力已对齐。
> 每个 step 仍遵循最小增量：单文件夹、单特性、三份规范、测试、提交。

### 6.1 差距清单（对照 nanobot SubagentManager）

| 组 | # | 缺口 | learn_nano 现状 | nanobot |
| --- | --- | --- | --- | --- |
| A 运行配置传播（优先） | G1 | `max_tool_result_chars` 未生效 | 子代理 `AgentRunSpec` 未传 `governance_config`，runner 用默认 16k；`tools.max_tool_result_chars` 被忽略 | 显式传 `max_tool_result_chars` |
| A | G2 | `fail_on_tool_error` 未生效 | 用默认 `False`，配置为 `True` 时不生效 | 传 `self.fail_on_tool_error` |
| A | G3 | `finalize_on_max_iterations` 语义差异 | 默认 `True`（生成收尾 fallback） | 子代理用 `False`（隐形续跑接管） |
| A | G4 | `max_iterations_message`/`error_message` 未显式传 | 用默认文案 | 显式传 |
| B runtime 同步 | G5 | 子代理用共享 `provider`，未传 per-parent `runtime`（模型/生成参数） | 共享 `self._provider` | 传 `runtime=runtime` |
| C announce 保真 | G6 | 未渲染 `subagent_announce.md` 模板；缺 `subagent_channel_display` body 清洗 | 内联 f-string | 模板渲染 + 通道清洗 |
| C | G8 | announce 未透传 `origin_message_id` | `_announce` 无该参数 | 透传以精准路由 |
| D API/上下文增强 | G7 | `spawn` 不支持 `temperature` 覆写 | 不支持 | `runtime.with_generation_overrides` |
| D | G9 | 子代理 `ToolContext` 未注入 `workspace_sandbox` | 未注入 | 注入 `workspace_sandbox_status(...)` |
| D | G10 | `SubagentStatus` 相位粒度不足 | 仅 `done`/`error` | `checkpoint_callback` 更新多相位 |

### 6.2 最小增量 step 规划

- **step120：子代理运行配置传播（G1–G4）** ✅ 已完成
  - 实现：从 `config.agents.defaults` 提取 `max_tool_result_chars` / `fail_on_tool_error`
    （缺省 16_000 / True），注入子代理 `AgentRunSpec`：`governance_config=ContextGovernanceConfig(
    tools=tools, max_tool_result_chars=..., context_window_tokens=200_000, max_tokens=4096)`
    （复刻 runner 默认预算，避免 `context_window_tokens=None` 触发全量工具结果摘要）、
    `fail_on_tool_error`、对齐 nanobot 硬编码的 `finalize_on_max_iterations=False` 与
    `max_iterations_message="Task completed but no final response was generated."`。
  - 文件：`subagent.py`（`_run_subagent` 的 `AgentRunSpec` + 两个 `_extract_*` 辅助）。
  - 测试：新增 `tests/test_subagent_run_config.py`（6 例）；全量失败数与 step119 基线持平（25）。

- **step121：announce 模板化 + origin_message_id 透传（G6 + G8）** ✅ 已完成
  - 实现：新增 `templates/agent/subagent_announce.md` 与零依赖 `{{ var }}` 渲染器，
    `_announce` 由内联 f-string 改为模板渲染（`status_text` 对齐 nanobot
    `"completed successfully"`/`"failed"`）；`tools/spawn.py` 的 `origin` 补全
    `origin_message_id`，`_announce` 非空时写入 `metadata["origin_message_id"]`。
  - 通道清洗（G6 通道部分）**推迟独立 step**：nanobot 在展示边界清洗且保留 LLM 全文，
    learn_nano 直接放 `_announce` 会截断 LLM 注入，故不在本 step 实现。
  - 文件：`subagent.py`、`tools/spawn.py`、`templates/agent/subagent_announce.md`。
  - 测试：新增 `tests/test_subagent_announce.py`（5 例）；全量失败数与 step120 基线持平（25）。

- **step122：runtime/model 逐父同步（G5）** ✅ 已完成
  - 实现（最小增量·衍生标量）：`_run_subagent` 从 `origin["runtime"]` 衍生
    `model`/`temperature`/`max_tokens` 注入 `AgentRunSpec`（`provider` 沿用 `self._provider`，
    生产环境 `self._provider == runtime.provider` 同对象，终态等价 nanobot）。
    **未新增 `AgentRunSpec.runtime` 字段、未改 `runner.py`**（经调研，runner 已把
    `spec.model/temperature/max_tokens` 转发给 `provider.chat_with_retry`，故改写标量即生效，
    且 `main.py` 接线使 provider 同对象，规避回归）。
  - 文件：`subagent.py`（`_run_subagent` 入口衍生 + `AgentRunSpec` 注入）。
  - 测试：新增 `tests/test_subagent_runtime_sync.py`（3 例）；全量失败数与 step121 基线持平（25）。
  - 刻意遗留：`context_window_tokens` 仍用 step120 的 `200_000`（防回归）；`provider` 取 `self._provider`。

- **step「通道清洗」（G6 通道部分，独立 step）**
  - 范围：实现 `utils/subagent_channel_display.py` 的 `scrub_subagent_announce_body`，在**展示边界**
    清洗 announce 正文（保留 LLM 注入所需的全文结果），供 channel 层复用；`_announce` 本体不截断。
  - 说明：step121 已推迟此部分；因 learn_nano 无独立展示管线，须先建清洗工具再在合适边界接线。

- **step123：子代理 ToolContext 沙箱 + 相位粒度（G9 + G10）**
  - 最小范围：`_build_tools` 注入 `workspace_sandbox=workspace_sandbox_status(...)`；
    `_run_subagent` 用 `checkpoint_callback` 更新 `status.phase` 多相位（initializing/awaiting_tools/...）。
  - 文件：`subagent.py`。
  - 测试：子代理 `ToolContext.workspace_sandbox` 已置；status.phase 在迭代中被更新为非终态。

- **step124：spawn temperature 覆写（G7）**
  - 最小范围：`spawn` 增加 `temperature` 参数，`runtime.with_generation_overrides(temperature=...)` 后下发。
  - 文件：`subagent.py`、`tools/spawn.py`。
  - 测试：传入 `temperature` 后子代理运行 runtime 的 `temperature` 被覆写。

### 6.3 推进顺序建议

1. **step120（必做）**：唯一影响运行行为正确性的缺口，优先一个 step 补齐。 ✅
2. **step121**：announce 模板 + origin_message_id 透传（通道清洗推迟独立 step）。 ✅
3. **step122**：runtime 逐父同步（G5，衍生标量方案，无 runner 改动）。 ✅
4. **step「通道清洗」**：G6 通道部分独立 step。
5. **step123 / step124**：打磨项，按需。

> 注：以上 step120–124 实施时均先写 proposal/design/api-spec 三份规范再落地。
