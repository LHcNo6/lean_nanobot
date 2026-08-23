# step124 接口契约（api-spec）

> 本文件定义 step124「spawn temperature 覆写（G7）」的对外契约。
> 改动范围：`llm.py`（`LLMRuntime` 方法）、`subagent.py`（`spawn` 形参）、`tools/spawn.py`（schema + execute）。

## A. LLMRuntime 方法契约（F1）

`step124/llm.py` 的 `LLMRuntime` 新增方法：

```python
def with_generation_overrides(
    self, *, temperature: float | None = None, max_tokens: int | None = None
) -> "LLMRuntime":
    """返回 generations 参数被覆写后的新运行设置（原对象不变）。"""
```

- 返回新 `LLMRuntime`：`provider` / `model` / `context_window_tokens` / `model_preset` /
  `snapshot_signature` 沿用原值；`generation` 重建为 `GenerationSettings(
  temperature=temperature if temperature is not None else 原值,
  max_tokens=max_tokens if max_tokens is not None else 原值,
  reasoning_effort=原值)`。
- 原 `LLMRuntime` 实例不可变（frozen），调用后不被修改。

## B. SubagentManager.spawn 契约（F2）

`SubagentManager.spawn`（step124/subagent.py）新增形参：

| 形参 | 类型 | 缺省 | 语义 |
| --- | --- | --- | --- |
| `temperature` | `float \| None` | `None` | 非空时覆写 `origin["runtime"]` 的 generation temperature |

覆写逻辑（在合并 `origin` 之后、启动后台任务之前）：
```python
if temperature is not None:
    rt = origin.get("runtime")
    if rt is None:
        rt = LLMRuntime(provider=self._provider, model="",
                        generation=GenerationSettings(), context_window_tokens=8192)
    origin["runtime"] = rt.with_generation_overrides(temperature=temperature)
```
- 旧的 `origin["runtime"]` 位置被覆写后的新 runtime 替换；其余 origin 字段不变。

## C. SpawnTool 契约（F3）

`step124/tools/spawn.py`：

- `tool_parameters_schema` 新增：
  ```python
  temperature=NumberSchema(0.7, description="...", minimum=0.0, maximum=2.0)
  ```
- `execute(self, task: str = "", label: str | None = None, temperature: float | None = None, **kwargs)`
  将 `temperature` 透传：`self._manager.spawn(task=task, label=label, origin=origin, temperature=temperature)`。

## D. 不变量

- `temperature=None` 时行为与 step123 完全一致（不触碰 `origin["runtime"]`）。
- `_run_subagent` / `runner.py` 不改；覆写经 step122（G5）既有「runtime → temperature 衍生」通道生效。
- `provider` 仍取 `self._provider`，不受 temperature 覆写影响。
