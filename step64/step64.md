# step64：`run_dream` 迁移到 harness

## 1. 问题背景

`run_dream` 是后台记忆整理功能，它构建 dream prompt 并直接调用 `runner.run`，不经过 `_process_message` 状态机。该方法最初放在 `AgentLoop` 中（step43），但从架构角度看，dream 是 harness 级别的后台任务，不应耦合在 `AgentLoop` 中。

nanobot 的架构中，dream 功能由 harness 层调度，loop 层不包含 dream 逻辑。step64 的目标是将 `run_dream` 从 `loop.py` 迁移到 harness（`main.py`），使架构更清晰。

## 2. 原理分析

### 2.1 为什么 dream 应该在 harness 层？

1. **职责分离**：`AgentLoop` 负责处理入站消息的状态机（`_process_message` → `_state_*`）。dream 是后台定时任务，不经过消息状态机，放在 loop 中是职责越界。

2. **调度权在 harness**：dream 的定时调度（`_dream_loop`）已经在 `main.py` 中。将 dream 逻辑也放在 harness 层，使调度和执行在同一层，更内聚。

3. **对齐 nanobot**：nanobot 的 dream 功能由 harness 层实现，loop 层不包含 dream。迁移后与 nanobot 架构对齐。

### 2.2 为什么不直接走 `process_direct`？

原计划是通过 `process_direct(ephemeral=True, tools=dream_tools)` 调用，但 `process_direct` 走的是 `_process_message` 状态机，而 dream 有特殊需求：
- 使用 `dream_key` 作为 session_key（格式为 `dream:YYYYMMDD-HHMMSS`）
- `max_iterations=15`（不同于默认值）
- 需要调用 `memory.set_last_dream_cursor` 更新游标
- 不需要持久化用户消息、不需要 hook 链、不需要流式响应

如果要走 `process_direct`，需要扩展该方法支持自定义 `max_iterations`、`session_key` 格式、后置回调等，改动较大。因此 step64 采取直接迁移逻辑的方案，保留 dream 的独立执行路径。

### 2.3 为什么 loop.py 中保留 deprecated 方法？

`command/builtin.py` 中的 `/dream` 命令在 loop 层调用 `ctx.loop.run_dream()`。如果直接移除该方法，会破坏命令功能。而 command 层不应导入 main 模块（会导致循环依赖：main → loop → command → main）。

因此 loop.py 中保留 `run_dream` 方法作为 deprecated 薄包装，实现逻辑与 `main.run_dream` 完全一致。未来 step 可以考虑将 `/dream` 命令也迁移到 harness 层，届时可完全移除 loop 中的方法。

## 3. 实现方案

### 3.1 main.py 中新增 `run_dream` 函数

```python
async def run_dream(loop: AgentLoop, tools: ToolRegistry | None = None) -> AgentRunResult | None:
    result = loop.memory.build_dream_prompt(max_entries=20)
    if result is None:
        return None
    prompt, last_cursor = result
    dream_key = f"dream:{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    spec = AgentRunSpec(
        initial_messages=[{"role": "user", "content": prompt}],
        tools=tools or loop.registry,
        provider=loop.provider,
        max_iterations=15,
        session_key=dream_key,
    )
    try:
        run_result = await loop._runner.run(spec)
        loop.memory.set_last_dream_cursor(last_cursor)
        return run_result
    except Exception:
        return None
```

该函数接收 `loop` 实例作为参数，使用 `loop.memory`、`loop.registry`、`loop.provider`、`loop._runner` 等属性。这是典型的"函数式迁移"——将方法逻辑提取为独立函数，接收原实例作为参数。

### 3.2 修改 `_dream_loop` 调用点

```python
async def _dream_loop(agent_loop: AgentLoop, interval_seconds: int):
    while True:
        await asyncio.sleep(interval_seconds)
        result = await run_dream(agent_loop)  # 改为调用模块级函数
        if result and result.final_content:
            print(f"\n[Dream] {result.final_content[:200]}\n")
```

### 3.3 loop.py 中标记 deprecated

更新 `run_dream` 方法的 docstring，说明已迁移到 `main.run_dream`，loop 层保留仅用于向后兼容。方法实现逻辑保持不变。

### 3.4 新增导入

main.py 中新增导入：
- `from step64.runner import AgentRunResult, AgentRunSpec`

## 4. 核心函数说明

### `main.run_dream(loop, tools=None)`

harness 层的 dream 执行函数。接收 `AgentLoop` 实例，使用其 memory/registry/provider/runner 属性完成 dream 流程。返回 `AgentRunResult` 或 None。

### `AgentLoop.run_dream(tools=None)`（deprecated）

loop 层的兼容方法。实现逻辑与 `main.run_dream` 完全一致，保留用于 `command/builtin.py` 的 `/dream` 命令。新代码应使用 `main.run_dream`。

## 5. 暴露问题与下一步

### 5.1 暴露的技术债

1. **loop.py 中仍保留 deprecated 方法**：由于 `command/builtin.py` 的 `/dream` 命令在 loop 层调用，无法完全移除。未来 step 可考虑将命令系统也迁移到 harness 层，或让 command 通过回调机制调用 harness 功能。

2. **`main.run_dream` 访问 `loop._runner` 私有属性**：函数式迁移后，`main.run_dream` 需要访问 `loop._runner`（下划线前缀的私有属性）。这在 Python 中是允许的，但不是最干净的设计。未来可以考虑在 `AgentLoop` 上暴露一个公共的 `run_spec(spec)` 方法。

3. **dream 逻辑重复**：`main.run_dream` 和 `AgentLoop.run_dream` 的实现逻辑完全一致，存在代码重复。未来移除 loop 中的 deprecated 方法后即可消除重复。

### 5.2 后续对齐路线

step61-64 完成了 agent 核心循环的对齐工作。后续对齐方向：
- **工具系统对齐**：learn_nano 仅有 7 个演示工具，nanobot 有 24 个生产级工具。需要逐步迁移核心工具（文件操作、shell 执行、网络请求等）。
- **Memory 系统对齐**：learn_nano 的 MemoryStore 较为简化，nanobot 的 memory 系统支持向量检索、摘要、分层存储等。
- **Subagent 系统对齐**：learn_nano 的 SubagentManager 是基础版，nanobot 支持并发子 agent、子 agent 通信、任务分解等高级功能。
- **Governance 系统对齐**：learn_nano 的治理模块较为基础，nanobot 支持权限控制、审计日志、安全策略等。
