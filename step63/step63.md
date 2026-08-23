# step63：消除 `_state_run` 重建 `_stream_spec` 的技术债

## 1. 问题背景

step61 为 `TurnContext` 新增了 `hooks/hook_factories/turn_scopes/tools` 四字段后，`_state_run` 中为了检查 `wants_streaming()` 并设置 `ctx.on_stream`，需要重新调用 `_build_agent_spec` 构建一个临时 `_stream_spec`。原代码注释承认这是技术债：

> `_run_agent_loop` 内部已构建 spec，这里重新构建一次以获取 hook（`_build_agent_spec` 是轻量级的，ToolLoader.load 对已注册工具幂等）。

虽然 `_build_agent_spec` 是轻量级的，但重复构建仍有以下问题：
1. **性能浪费**：每次 turn 结束都要重新构建一次 spec，包括 hook 链组装、工具加载、workspace scope 计算。
2. **一致性风险**：如果 `_run_agent_loop` 内部和 `_state_run` 重建时传入的参数不一致，可能导致 hook 行为不一致。step61 已经需要手动同步 `hooks/hook_factories/tools` 等参数。
3. **代码冗余**：`_state_run` 中需要重复传入大量参数给 `_build_agent_spec`，与 `_run_agent_loop` 内部的调用几乎完全相同。

## 2. 原理分析

### 2.1 nanobot 是怎么做的？

nanobot 的 `_run_agent_loop` 返回值中包含了 `hook` 相关信息，`_state_run` 不需要重新构建 spec。具体来说，nanobot 的 runner 运行后会返回 `AgentRunResult`，其中包含 `streaming_hook` 或类似字段。

### 2.2 为什么不直接改变返回值？

`_run_agent_loop` 的返回值是 `tuple[str | None, list[str], list[dict[str, Any]], str, bool]`，即 `(final_content, tools_used, messages, stop_reason, had_injections)`。改变返回值长度会破坏所有调用方（包括 `_process_system_message`、`_state_run` 和大量测试）。

### 2.3 实例属性方案

最小增量的方案是：在 `_run_agent_loop` 中构建 spec 后，将 `spec.hook` 保存到实例属性 `self._last_turn_hook`，然后 `_state_run` 从实例属性中读取 hook，不再重建 `_stream_spec`。

这个方案的优点：
- **最小改动**：不需要改变返回值，不需要修改调用方。
- **消除重复构建**：`_state_run` 不再调用 `_build_agent_spec`。
- **保证一致性**：使用的是 `_run_agent_loop` 内部实际构建的 hook，不存在参数不一致的风险。

缺点：
- **实例状态**：引入了实例属性 `_last_turn_hook`，需要注意并发安全（但 loop 本身是单 turn 顺序执行的，不存在并发问题）。

## 3. 实现方案

### 3.1 `_run_agent_loop` 中保存 hook

在 `_run_agent_loop` 中，构建 spec 后、调用 `runner.run(spec)` 之前，保存 hook：

```python
# step63：保存本次 turn 的 hook 到实例属性，供 _state_run 读取，
# 消除 _state_run 重建 _stream_spec 的技术债。
self._last_turn_hook = spec.hook
result = await self._runner.run(spec)
```

### 3.2 `_state_run` 中读取实例属性

替换原来的重建 `_stream_spec` 代码块：

```python
# step63：从 _run_agent_loop 保存的 hook 读取，不再重建 _stream_spec。
_last_hook = getattr(self, '_last_turn_hook', None)
if _last_hook is not None and _last_hook.wants_streaming():
    ctx.on_stream = _last_hook
```

使用 `getattr` 带默认值 `None`，保证在 `_run_agent_loop` 未被调用的情况下不会报错。

## 4. 核心函数说明

### `_run_agent_loop` 中的变更

在构建 `spec` 后新增一行 `self._last_turn_hook = spec.hook`，将本次 turn 的 hook 对象保存到实例属性。这是唯一的代码变更点。

### `_state_run` 中的变更

删除了约 30 行的 `_build_agent_spec` 调用（包括所有参数传递），替换为 4 行的实例属性读取。代码量大幅减少，且消除了重复构建。

## 5. 暴露问题与下一步

### 5.1 暴露的技术债

1. **实例属性不是最干净的设计**：`_last_turn_hook` 是实例级状态，理论上应该通过返回值传递。未来 step 可以考虑将 `_run_agent_loop` 的返回值改为 dataclass 或命名元组，包含 `hook` 字段，从而移除实例属性。

2. **`_build_agent_spec` 仍被测试直接调用**：虽然 `_state_run` 不再调用 `_build_agent_spec`，但很多测试仍直接调用该方法来验证 spec 构建逻辑。这是合理的，`_build_agent_spec` 作为独立方法仍有测试价值。

3. **`_process_system_message` 不设置 `ctx.on_stream`**：`_process_system_message` 调用 `_run_agent_loop` 后不检查 `wants_streaming()`，这是因为 system message 路径不需要流式响应。这是预期行为，不是问题。

### 5.2 下一步规划

- **step64**：将 `run_dream` 从 `loop.py` 迁移到 harness（`main.py`），通过 `process_direct(ephemeral=True, tools=dream_tools)` 调用。这将进一步简化 `loop.py`，使 dream 功能走统一的 `process_direct` 路径。
