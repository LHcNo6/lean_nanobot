# Step 38 — 配置层接入 `max_tool_iterations`

## 解决了什么问题及为什么

step36 新增了 `self.max_iterations` 属性，但默认值硬编码为 5（`__init__` 参数默认值），`from_config` 装配时未从配置读取 `max_tool_iterations`，导致生产环境也使用 5 次迭代上限，与 nanobot 的 200 次不一致。

nanobot 通过 `AgentDefaults.max_tool_iterations = 200` 配置默认值，并在 `from_config` 中显式传递 `max_iterations=defaults.max_tool_iterations`。

本 step 对齐这一设计：`from_config` 从 `config.agents.defaults.max_tool_iterations` 读取并传递给 `AgentLoop`，使生产环境使用配置值（默认 200），同时保持直接构造时的默认值 5（测试友好）。

## 目标和实现

### 目标
- `from_config` 装配时使用配置中的 `max_tool_iterations`（默认 200）；
- 支持配置自定义（如 `max_tool_iterations: 50`）；
- 支持 `extra` 覆盖（测试中传入小值）；
- 不破坏直接构造时的默认值 5。

### 实现

#### `from_config` 传递 `max_iterations`（loop.py:340-343）

```python
return cls(
    ...
    # step38：配置层接入 max_tool_iterations（默认 200），
    # 替代 __init__ 硬编码默认 5；extra 可覆盖（测试用小值）。
    max_iterations=extra.pop("max_iterations", defaults.max_tool_iterations),
    ...
)
```

- `defaults.max_tool_iterations` 来自 `config.agents.defaults`，默认 200；
- `extra.pop("max_iterations", ...)` 允许调用者通过 `extra` 覆盖；
- 先 `pop` 再 `**extra`，避免重复参数。

## 核心函数/类功能说明

| 函数/字段 | 位置 | 功能 |
|----------|------|------|
| `AgentDefaults.max_tool_iterations` | config/schema.py:81 | 配置默认迭代上限，默认 200 |
| `AgentLoop.from_config` | loop.py:294 | 从 Config 装配 AgentLoop，传递配置参数 |
| `AgentLoop.max_iterations` | loop.py:223 | 单 turn 最大工具迭代次数 |

## 设计决策

### 为什么保持 `__init__` 默认值 5 而不是改为 200？

| 场景 | 默认 5 | 默认 200 |
|------|--------|----------|
| 测试中直接构造 `AgentLoop()` | 快速触发 max_iterations 边界 | 需 200 次迭代，测试慢 |
| 生产环境 `from_config(config)` | 配置驱动为 200 | 配置驱动为 200 |
| 现有测试兼容性 | 不破坏 | 需修改 `test_default_max_iterations_is_5` 等 |

**结论**：保持 `__init__` 默认 5，`from_config` 传递配置值 200。测试用小值，生产用配置值，最小增量不破坏现有测试。

### 为什么不添加 `__init__` 中的 `defaults` 回退逻辑？

nanobot 用 `max_iterations: int | None = None` + `defaults = AgentDefaults()` 回退，学习版保持简单：`__init__` 默认 5，`from_config` 显式传递即可。

## 测试

新增 `tests/test_config_max_iterations.py`，6 个测试：

| 测试名 | 验证点 |
|--------|--------|
| `test_default_config_uses_200` | 默认配置 from_config 后 loop.max_iterations == 200 |
| `test_custom_config_overrides` | 配置中 max_tool_iterations=50 时生效 |
| `test_extra_override` | from_config(max_iterations=3) 时为 3 |
| `test_direct_init_default_5` | 直接构造 AgentLoop() 默认仍为 5 |
| `test_build_agent_spec_uses_config_value` | from_config 构造的 loop 的 spec 使用配置值 |
| `test_camel_case_config_key` | 驼峰键 maxToolIterations 也能正确读取 |

全量测试：**396 passed**（step37: 390，新增 6），运行时间 11.15s。

## 暴露了什么问题

1. **`__init__` 默认值与配置值不一致**：直接构造默认 5，配置装配默认 200。这是有意设计（测试友好 vs 生产合理），但需在文档中明确。
2. **`SubagentManager` 未配置驱动**：`SubagentManager` 的 `max_iterations` 默认 10，`from_config` 中未构造 `SubagentManager`（外部传入），留待后续对齐。
3. **`dream` 方法硬编码 15**：独立路径，暂不修改。
4. **`__init__` 无 `defaults` 回退**：nanobot 用 `None` 回退到 `AgentDefaults().max_tool_iterations`，学习版保持简单。

## 下一 step 要解决什么

- **step39**：`file_state` contextvar 绑定（文件状态上下文）；
- **step40**：`turn_scopes` + `hook_factories`（turn 级 context manager + hook 工厂）；
- **step41**：`ephemeral` 模式 + `run_extra_hooks_for_ephemeral`（临时运行模式）；
- **step42+**：MCP Integration、真实通道、cron/local trigger、memory 文件系统/dream。

## 与 nanobot 对齐度

| 维度 | step37 | step38 | nanobot |
|------|--------|--------|---------|
| `AgentDefaults.max_tool_iterations` | ✅ | ✅ | ✅ |
| `from_config` 传递 max_iterations | ❌ | ✅ | ✅ |
| 配置驱动的 max_iterations | ❌ | ✅ | ✅ |
| `__init__` 默认 None + 回退 | ❌（默认5） | ❌（保持5） | ✅ |
| `SubagentManager` 配置驱动 | ❌ | ❌（不做） | ✅ |
| `dream` 方法配置驱动 | ❌ | ❌（不做） | N/A |
