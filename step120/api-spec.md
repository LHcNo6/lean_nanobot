# step120 接口契约（api-spec）

> 本文件定义 step120「子代理运行配置传播」的对外契约。改动范围仅限
> `SubagentManager` 内部，无新增公开 API、无新增配置 schema 字段。

## A. 配置输入契约（已有 schema，未新增字段）

| 配置路径 | 类型 | 缺省 | 含义 |
| --- | --- | --- | --- |
| `config.agents.defaults.max_tool_result_chars` | `int` (ge=0) | `16_000` | 工具结果截断字符数 |
| `config.agents.defaults.fail_on_tool_error` | `bool` | `True` | 工具错误是否升级为运行失败 |

> G3/G4 无对应 config 字段，按 nanobot 硬编码（见 E 节）。

## B. 提取辅助函数（模块内私有）

```python
def _extract_max_tool_result_chars(config: Any) -> int:
    """返回 config.agents.defaults.max_tool_result_chars；缺失回退 16_000。"""

def _extract_fail_on_tool_error(config: Any) -> bool:
    """返回 config.agents.defaults.fail_on_tool_error；缺失回退 True。"""
```

- 入参 `config`：完整 `Config` / 扁平 duck-view / `None`（同 `_extract_disabled_skills`）。
- 出参：`int` / `bool`，永不抛错（缺失链任意节点返回缺省）。

## C. `SubagentManager` 新增内部属性

| 属性 | 类型 | 来源 |
| --- | --- | --- |
| `self._max_tool_result_chars` | `int` | `_extract_max_tool_result_chars(config)` |
| `self._fail_on_tool_error` | `bool` | `_extract_fail_on_tool_error(config)` |

## D. 运行期注入契约（每次 `_run_subagent`）

`AgentRunSpec` 构造增加：

```python
AgentRunSpec(
    ...
    governance_config=ContextGovernanceConfig(
        tools=tools,                              # 子代理工具注册表
        max_tool_result_chars=self._max_tool_result_chars,
        context_window_tokens=200_000,           # 复刻 runner 默认，避免预算为 0
        max_tokens=4096,                         # 复刻 runner 默认
    ),
    fail_on_tool_error=self._fail_on_tool_error,
    finalize_on_max_iterations=False,            # 对齐 nanobot 硬编码
    max_iterations_message="Task completed but no final response was generated.",
)
```

## E. 行为语义契约

- **E1**：子代理工具结果按 `max_tool_result_chars` 截断（经 `ContextGovernor.normalize_tool_result`）。
- **E2**：子代理工具调用抛错时，若 `fail_on_tool_error` 为 `True`，运行以错误终止（announce
  error）；为 `False` 则继续。
- **E3**：子代理触达 `max_iterations` 时不生成收尾 fallback（由隐形续跑接管）。
- **E4**：`max_iterations_message` 使用 nanobot 同款文案常量。

## F. 不变量

- 既有子代理安全不变量（scope 隔离、无递归 spawn、owner 会话隔离、announce 回注）全部保持不变。
- `context_window_tokens=200_000` 与 prior（governance_config=None 时 runner 默认）一致，不引入
  历史裁剪/摘要行为回归。
