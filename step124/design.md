# step124 架构设计：spawn temperature 覆写（G7）

## 1. 总体设计

G7 是「运行期生成参数覆写」能力，分三层落地：

- **运行时层（F1）**：`LLMRuntime`（`step124/llm.py`）新增不可变覆写方法
  `with_generation_overrides`，对齐 nanobot `utils/llm_runtime.py` 的同名方法。
- **管理器层（F2）**：`SubagentManager.spawn`（`step124/subagent.py`）接纳 `temperature` 形参，
  在合并 `origin` 后、启动后台任务前，将覆写后的 runtime 写回 `origin["runtime"]`。
- **工具层（F3）**：`SpawnTool`（`step124/tools/spawn.py`）在参数 schema 暴露 `temperature`，
  由 LLM 调用时传入，并透传给 `manager.spawn`。

核心原则：**最小增量 + 复用 G5 通道**。step122 已让 `_run_subagent` 从 `origin["runtime"]` 衍生
`temperature` 注入 `AgentRunSpec`；本 step 只需在 spawn 侧把「覆写后的 runtime」放入 origin，
覆写即沿既有 G5 通道自动生效，**不改 `_run_subagent` / runner**。

## 2. 关键原理

### 2.1 LLMRuntime.with_generation_overrides（F1）

- `LLMRuntime` 是 `frozen` dataclass（`llm.py:27`），含 `provider` / `model` / `generation` /
  `context_window_tokens` / `model_preset` / `snapshot_signature`；`temperature`/`max_tokens`
  为读取 `generation` 的 property。
- 覆写方法返回**新实例**：`generation=GenerationSettings(temperature=新值, max_tokens=新值,
  reasoning_effort=沿用)`，其余字段原样复制。`frozen` + 新建保证原对象不可变、不被污染。
- 仅覆写非空参数：调用方传 `temperature=0.3` 时 `max_tokens` 保持原值。

### 2.2 spawn 覆写 runtime（F2）

```python
async def spawn(self, task, label=None, origin_channel="cli", origin_chat_id="direct",
               session_key=None, *, origin=None, temperature: float | None = None):
    ...
    merged = dict(origin or {})
    ...
    if temperature is not None:
        rt = merged.get("runtime")
        if rt is None:
            # 无父 runtime 时以 manager provider 合成最小 runtime 兜底，保证覆写生效
            rt = LLMRuntime(provider=self._provider, model="",
                           generation=GenerationSettings(), context_window_tokens=8192)
        merged["runtime"] = rt.with_generation_overrides(temperature=temperature)
    origin = merged
    ...
    bg_task = asyncio.create_task(self._run_subagent(task_id, task, display_label, origin, status))
```

### 2.3 _run_subagent 自动衍生（G5 通道，无需改动）

`_run_subagent` 已有（step122）：
```python
runtime = origin.get("runtime") if origin else None
...
temperature = getattr(runtime, "temperature", 0.7)
```
覆写后 `origin["runtime"].temperature` 为新值 → `spec.temperature` 即覆写值。provider 仍取
`self._provider`（生产同 `runtime.provider` 同对象），`model` 仍从 runtime 继承，仅 temperature 被覆写。

### 2.4 SpawnTool 暴露参数（F3）

`tools/spawn.py`：
```python
@tool_parameters(tool_parameters_schema(
    task=...,
    label=...,
    temperature=NumberSchema(0.7, description="...", minimum=0.0, maximum=2.0),
    required=["task"],
))
class SpawnTool(Tool):
    async def execute(self, task="", label=None, temperature=None, **kwargs):
        ...
        result = await self._manager.spawn(task=task, label=label, origin=origin, temperature=temperature)
```

## 3. 改动文件清单

- `step124/llm.py`：`LLMRuntime.with_generation_overrides`。
- `step124/subagent.py`：import `LLMRuntime`/`GenerationSettings`；`spawn` 加 `temperature` 形参并覆写 runtime。
- `step124/tools/spawn.py`：schema 加 `temperature`；`execute` 透传。
- 新增 `step124/tests/test_subagent_temperature_override.py`。
- 不改 `runner.py` / `_run_subagent` / `loop.py`。

## 4. 测试策略

- `test_llm_runtime_with_generation_overrides`：覆写后 `temperature` 改变、`model`/`provider` 不变、原对象不变。
- `test_spawn_applies_temperature_override`：subagent manager 置 provider；`origin` 含 runtime；
  `spawn(task, origin=origin, temperature=0.3)`；假 runner 捕获 spec → `spec.temperature == 0.3`、
  `spec.model` 仍为 runtime 原 model、`spec.provider is self._provider`。
- `test_spawn_no_override_keeps_runtime_temperature`：`temperature=None` → `spec.temperature` 保持 runtime 原值。
- `test_spawntool_declares_temperature`：`SpawnTool` schema 含 `temperature` 参数。

## 5. 验收标准

- 全量 `step124/tests` 失败数与 step123 基线（25）持平。
- 新增用例全绿。
- 配套 `step124.md` + 三份规范齐备，提交并推送。
