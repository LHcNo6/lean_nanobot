# Step 36 — `self.max_iterations` 属性 + `_sync_subagent_runtime_limits`

## 解决了什么问题及为什么

step35 提取了 `_run_agent_loop` 方法，但 `max_iterations` 仍然硬编码在 `_build_agent_spec` 中（值为 5），且 `SubagentManager` 的 `max_iterations`（默认 10）与 loop 级设置不一致。

nanobot 通过 `self.max_iterations` 属性统一管理迭代上限，并在每次 turn 运行前通过 `_sync_subagent_runtime_limits` 同步到 subagent 管理器，确保 spawn 出的 subagent 使用相同的迭代上限。

本 step 对齐这一设计：
1. `AgentLoop.__init__` 新增 `max_iterations` 参数和 `self.max_iterations` 属性；
2. `_build_agent_spec` 改用 `self.max_iterations`（替代硬编码 5）；
3. 新增 `_sync_subagent_runtime_limits` 方法，同步 subagent 的 `max_iterations`；
4. `_run_agent_loop` 开头调用 sync，max_iterations 终止时记录警告日志。

## 目标和实现

### 目标
- 消除 `_build_agent_spec` 中的硬编码 `max_iterations=5`；
- loop 级 `max_iterations` 可配置；
- subagent 的 `max_iterations` 与 loop 保持一致；
- max_iterations 终止时记录警告日志（对齐 nanobot）。

### 实现

#### 1. `AgentLoop.__init__` 新增参数（loop.py:183, 217）
```python
def __init__(self, ..., max_iterations: int = 5) -> None:
    ...
    self.max_iterations = max_iterations  # step36: 替代 _build_agent_spec 硬编码 5
```
- 默认值 5（保持学习版轻量，不改为 nanobot 的 200，避免测试超时）；
- 配置层接入（读取 `AgentDefaults.max_tool_iterations`）留待后续 step。

#### 2. `_sync_subagent_runtime_limits` 方法（loop.py:931-944）
```python
def _sync_subagent_runtime_limits(self) -> None:
    """将 loop 级运行时限制同步到 subagent 管理器（step36，对齐 nanobot）。"""
    if self.subagents is None:
        return
    self.subagents.max_iterations = self.max_iterations
```
- `self.subagents` 为 None 时（测试中常不传入）直接返回，不报错；
- 目前仅同步 `max_iterations`（nanobot 也只同步这一项）。

#### 3. `_run_agent_loop` 修改（loop.py:1283, 1310-1311）
- 开头调用 `self._sync_subagent_runtime_limits()`（对齐 nanobot loop.py:798）；
- max_iterations 终止时添加 `logger.warning("Max iterations (%d) reached", self.max_iterations)`（对齐 nanobot loop.py:966）。

#### 4. `_build_agent_spec` 改用 `self.max_iterations`（loop.py:1021）
```python
# 原：max_iterations=5,
max_iterations=self.max_iterations,  # step36: 替代硬编码 5
```

#### 5. `effective_stream` bug 修复（loop.py:1299-1313）
step35 遗留 bug：`effective_stream` 被赋值为 `spec.hook`（hook 对象不可调用），导致 max_iterations stream 推送时 `TypeError`。
修复：从 hook 中提取可调用的 `_on_stream` / `_on_stream_end` 回调；`CompositeHook` 时遍历子 hook 找到 `AgentProgressHook`。

## 核心函数/类功能说明

| 函数/属性 | 位置 | 功能 |
|----------|------|------|
| `AgentLoop.max_iterations` | loop.py:217 | 单 turn 最大工具迭代次数，默认 5 |
| `AgentLoop._sync_subagent_runtime_limits()` | loop.py:931 | 将 `self.max_iterations` 同步到 `self.subagents.max_iterations` |
| `AgentProgressHook._on_stream` | hook.py:400 | 流式内容回调（可调用） |
| `AgentProgressHook._on_stream_end` | hook.py:401 | 流式结束回调（可调用） |

## 测试

新增 `tests/test_max_iterations.py`，13 个测试，5 个测试类：

| 测试类 | 测试数 | 覆盖点 |
|--------|--------|--------|
| `TestMaxIterationsAttribute` | 3 | 默认值 5 / 自定义值 / 较大值 |
| `TestSyncSubagentRuntimeLimits` | 4 | 同步 / None 安全 / 覆盖默认值 / 幂等 |
| `TestBuildAgentSpecUsesMaxIterations` | 3 | spec 使用 self.max_iterations / 自定义值 / 无硬编码 |
| `TestMaxIterationsWarning` | 1 | max_iterations 时记录警告日志 |
| `TestRunAgentLoopCallsSync` | 2 | _run_agent_loop 调用 sync / None subagents 不报错 |

全量测试：**378 passed**（step35: 365，新增 13），运行时间 8.90s。

## 暴露了什么问题

1. **`effective_stream` bug**：step35 中 `effective_stream = spec.hook` 将 hook 对象赋值给可调用变量，导致 max_iterations stream 推送时 `TypeError`。step35 测试未触发（因硬编码 max_iterations=5 且测试脚本不足），step36 改为 `self.max_iterations` 后测试触发，已修复。
2. **默认值差异**：nanobot 默认 `max_iterations=200`（`AgentDefaults.max_tool_iterations`），学习版保持 5。配置层接入留待后续 step。
3. **`dream` 方法硬编码**：`dream` 方法中 `max_iterations=15` 仍为硬编码，独立路径，暂不修改。
4. **`SubagentManager` 构造时未传递**：`AgentLoop.__init__` 中 `subagent_manager` 由外部传入，其 `max_iterations` 可能与 loop 不一致，通过运行时 `_sync_subagent_runtime_limits` 同步。

## 下一 step 要解决什么

- **step37**：`llm_timeout_s` + `runner_wall_llm_timeout_s`（持续目标 turn 禁用 LLM 超时）；
- **step38**：配置层接入 `max_tool_iterations`（装配代码读取配置，默认值改为 200）；
- **step39**：`file_state` contextvar 绑定（文件状态上下文）；
- **step40**：`turn_scopes` + `hook_factories`（turn 级 context manager + hook 工厂）。

## 与 nanobot 对齐度

| 维度 | step35 | step36 | nanobot |
|------|--------|--------|---------|
| `AgentLoop.max_iterations` 属性 | ❌ | ✅ | ✅ |
| `_sync_subagent_runtime_limits` 方法 | ❌ | ✅ | ✅ |
| `_run_agent_loop` 调用 sync | ❌ | ✅ | ✅ |
| `_build_agent_spec` 使用 self.max_iterations | ❌（硬编码5） | ✅ | ✅ |
| max_iterations 警告日志 | ❌ | ✅ | ✅ |
| `effective_stream` 可调用修复 | ❌（bug） | ✅ | ✅ |
| 默认值 200 | N/A | ❌（保持5） | ✅ |
| 配置层接入 | ❌ | ❌（不做） | ✅ |
| `dream` 方法使用 self.max_iterations | ❌（硬编码15） | ❌（不做） | N/A |
