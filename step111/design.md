# Step111 架构设计：Subagent 工具集隔离

## 1. 原理分析

### 1.1 nanobot 参考实现（对齐锚点）

`nanobot/agent/subagent.py`：

```python
def _subagent_tools_config(self) -> ToolsConfig:          # :188-195
    """子代理只搬 exec/web/file 三组配置。"""
    return ToolsConfig(exec=..., web=..., file=..., restrict_to_workspace=...)

def _build_tools(self, workspace=None, tools_config=None) -> ToolRegistry:   # :197-217
    registry = ToolRegistry()
    ctx = ToolContext(
        config=cfg, workspace=str(root.resolve()),
        exec_session_manager=self._exec_session_manager,   # 跨子代理共享
        file_state_store=FileStates(),                     # 每次构建全新
        workspace_sandbox=...,
    )
    ToolLoader().load(ctx, registry, scope="subagent")     # ← 防递归的关键一行
    return registry                                        # _run_subagent :302 每次 spawn 调用
```

三个设计要点：
1. **准入即裁剪**：工具过滤发生在注册表构建期，而非运行期拦截；
2. **共享 ExecSessionManager、独立 FileStates**：前者让长命令会话可被 manager 统一
   管理，后者保证并发子代理的 read-dedup/read-before-edit 状态互不污染；
3. **每次 spawn 构建**：为将来 per-spawn 的 workspace_scope 差异化留出结构空间。

### 1.2 learn_nano 现状与差距

- `ToolLoader.load(ctx, registry, scope="core")` 已支持 scope 参数（loader.py:55-60），
  过滤规则 `scope not in tool_cls._scopes → skip`；
- 11 个工具已声明 `_scopes ⊇ {"subagent"}`：
  | 工具 | 文件 |
  |------|------|
  | exec | tools/shell.py:90 |
  | read_file / write_file / edit_file / list_dir | tools/filesystem.py |
  | find_files / grep | tools/search.py |
  | web_search / web_fetch | tools/web.py |
  | write_stdin | tools/exec_session.py:463 |
  | apply_patch | tools/apply_patch.py:182 |

  > nanobot 子代理为 13 工具（多 `cli_apps`、`list_exec_sessions`，
  > learn_nano 未实现这两个工具类），本 step 以现有全集为准。
- spawn/message/create_goal/update_goal/self/cron/mcp/image_generation/glob_tool/
  echo 均默认 `{"core"}` 或显式不含 subagent；
- **缺口**：SubagentManager 不经 loader，直接持有主 registry（构造参数 `tools`）。

### 1.3 关键实现约束（调查结论）

1. **配置口径**：工具的 `enabled()/create()` 按**扁平视图**读取配置——根级
   `.web`、`.exec`、`.tools`（tests 中 SimpleNamespace 即此形态）。真实 pydantic
   `Config` 只有根级 `.tools`（web/exec 嵌套其内），直接传入会使 web/exec 组在
   loader 的静默异常处理下被跳过。因此 `_build_tools` 必须先做**扁平化适配**。
2. **exec_session 双保险**：`WriteStdinTool.enabled` 要求 `ctx.exec_session_manager`
   非 None；同时本 step 的 ToolContext **不注入 bus/subagent_manager/sessions**，
   即使未来误把 spawn 类工具标上 subagent scope，其 create() 也会因缺依赖失败而被
   loader 跳过——双保险防递归。
3. **file_state_store.for_session(None)**：子代理尚未绑定 session_key（后续 step），
   store 对 None key 返回匿名 FileStates 实例，行为安全。
4. **loader 静默跳过**：load 循环 `except Exception: continue`，任何 enabled/create
   异常都会导致工具缺席——测试用例 ①（恰含 11 工具）作为该风险的兜底断言。

## 2. 方案

### 2.1 SubagentManager 接口变更

```python
class SubagentManager:
    def __init__(self, bus, provider=None, *, config=None, workspace="",
                 restrict_to_workspace=None, max_concurrent_subagents=5,
                 max_iterations=10):
```

- 移除 `tools: ToolRegistry | None` 参数（破坏性变更，同步改 main.py 与测试）；
- 新增 `config`：完整 Config / 扁平 duck-view / None（内部统一适配）；
- 新增 `workspace`：子代理工具的工作区根（str）；
- 新增 `restrict_to_workspace=None`：显式 True/False 优先；None 时回落
  `config.tools.restrict_to_workspace`；
- 惰性持有 `self._exec_session_manager = ExecSessionManager()`（跨子代理共享）。

### 2.2 配置扁平化适配器

模块级私有函数 `_flatten_tools_config(config) -> Any`：

| 输入形态 | 判定 | 输出 |
|----------|------|------|
| 完整 pydantic Config | 有 `.tools` 且无根级 `.web` | `SimpleNamespace(web=cfg.tools.web, exec=cfg.tools.exec, tools=<覆写 restrict 后的 tools 视图>)` |
| 已扁平 duck-view（测试用 SimpleNamespace） | 同时有 `.tools` 与 `.web` | 原样返回 |
| None / 其他 | — | 从空 `ToolsConfig()` 构造扁平视图 |

`restrict_to_workspace` 显式参数非 None 时，覆写视图内 `tools.restrict_to_workspace`，
保证文件类工具的边界行为以装配意图为准。

### 2.3 _build_tools 与调用点

```python
def _build_tools(self) -> ToolRegistry:
    registry = ToolRegistry()
    tool_ctx = ToolContext(
        config=self._flattened_config(),
        workspace=str(Path(self._workspace).resolve()) if self._workspace else "",
        restrict_to_workspace=self._restrict_to_workspace,
        # 故意不填：bus / subagent_manager / sessions / session_key / cron_*
        exec_session_manager=self._exec_session_manager,
        file_state_store=FileStateStore(),      # 每次构建全新（子代理间隔离）
    )
    ToolLoader().load(tool_ctx, registry, scope="subagent")
    return registry

async def _run_subagent(...):
    ...
    result = await self.runner.run(AgentRunSpec(
        initial_messages=messages,
        tools=self._build_tools(),              # ← 替换原 self._tools
        ...
    ))
```

每次 spawn 构建一次（用户决策 + 对齐 nanobot :302 调用点）；构建成本低
（纯对象实例化 + 包内 import 缓存命中），无 I/O。

### 2.4 main.py 装配变更

```python
subagent_manager = SubagentManager(
    bus=bus,
    provider=snapshot.provider,
    config=config,
    workspace=workspace,
    max_concurrent_subagents=defaults.max_concurrent_subagents,
)
# 删除 tools=registry
```

主 registry 的装配顺序不变（SpawnTool 等仍手动注册到主 registry）。

## 3. 数据流（spawn 后）

```
LLM(spawn) → SpawnTool.execute → manager.spawn → asyncio.create_task(_run_subagent)
    → _build_tools(): ToolRegistry(13)            ← 本 step 核心
    → AgentRunner.run(spec.tools=registry(13))
        → 子代理 LLM 只看到 11 个工具 schema
        → 若试图调 spawn：schema 不存在 → provider/runner 层报未知工具
    → _announce 结果回注 bus（不变）
```

## 4. 风险与缓解

| 风险 | 缓解 |
|------|------|
| loader 静默吞掉 enabled/create 异常导致工具悄悄缺失 | 用例① 恰含 11 工具的全量断言兜底；design 记录该陷阱 |
| 真实 Config 与扁平视图口径不一致 | `_flatten_tools_config` 统一适配；三种输入形态均有测试 |
| 移除 `tools` 参数破坏既有测试 | 既有测试均未传 tools（已核实 test.py/tests 全部构造点），回归风险低 |
| 共享 ExecSessionManager 导致子代理会话串扰 | 与 nanobot 同语义（共享是有意行为）；后续 step 如需隔离再评估 |

## 5. 测试设计

见 api-spec.md §测试契约。核心六用例覆盖：白名单完整性、黑名单排除、
端到端递归拒绝、组级开关、file_state 隔离、既有回归。
