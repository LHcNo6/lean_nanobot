# Step 5 — AgentRunner 单轮工具调用

## 目标

把 Provider（step3）和 ToolRegistry（step4）串起来，实现完整的 LLM 工具调用循环。

## 文件结构

```
step5/
├── __init__.py
├── llm.py                    # LLMResponse / ToolCallRequest / RetryConfig（from step3）
├── provider.py               # LLMProvider ABC + retry（from step3）
├── openai_compat_provider.py # OpenAICompatProvider（from step3）
├── tool.py                   # Tool(ABC) / ToolResult / ToolRegistry（from step4）
├── tools/echo.py             # EchoTool（from step4）
├── runner.py                 # ★ NEW: AgentRunSpec + AgentRunResult + AgentRunner
├── main.py                   # ★ NEW: CLI 演示完整工具调用
├── test.py                   # ★ NEW: 9 个测试
└── step5.md                  # 本文档
```

## 核心流程

```
AgentRunner.run(spec):
  messages = spec.initial_messages
  for iteration in range(max_iterations):
    1. provider.chat_with_retry(messages, tools=get_definitions())
    2. Response:
       ├─ tool_calls + finish_reason="tool_calls"?
       │   ├─ build assistant message with tool_calls → append
       │   ├─ for each tc: registry.execute(name, **params) → result
       │   ├─ append {"role": "tool", ...} result messages
       │   └─ continue (next iteration)
       └─ 文本?
           ├─ append assistant message
           └─ return AgentRunResult(final_content=..., stop_reason="stop")
  超出迭代次数 → return AgentRunResult(stop_reason="max_iterations")
```

## 核心接口

### AgentRunSpec

```python
@dataclass
class AgentRunSpec:
    initial_messages: list[dict]    # 起始消息（通常 [user]）
    tools: ToolRegistry             # 可用工具
    provider: LLMProvider           # LLM 提供商
    max_iterations: int = 10        # 最大工具调用轮数
    model: str | None = None
    temperature: float = 0.7
    max_tokens: int = 4096
```

### AgentRunResult

```python
@dataclass
class AgentRunResult:
    final_content: str | None       # 最终文本
    messages: list[dict]            # 完整消息历史
    tools_used: list[str]           # 使用的工具名列表
    usage: dict[str, int]           # total_prompt_tokens / total_completion_tokens
    stop_reason: str                # "stop" / "max_iterations" / ...
```

## 消息格式

Assistant 消息（OpenAI wire format）：
```python
{"role": "assistant", "content": None, "tool_calls": [
    {"id": "call_1", "type": "function",
     "function": {"name": "echo", "arguments": '{"text":"hi"}'}}
]}
```

Tool result 消息：
```python
{"role": "tool", "tool_call_id": "call_1", "name": "echo", "content": "Echo: hi"}
```

## 与 nanobot 对比

| 功能 | nanobot | step5 | 计划 |
|---|---|---|---|
| 流式 | chat_stream_with_retry | 仅 chat_with_retry | Step 12 |
| Hook 系统 | before/after run/iteration/execute | 无 | Step 11 |
| Context Governance | 修复/压缩/回填 | 无 | Step 14 |
| 注入消息 | injection_callback | 无 | Step 13 |
| 目标延续 | goal_active_predicate | 无 | Step 16 |
| 并发工具 | concurrent_tools | 顺序执行 | 暂不实现 |
| Usage 累积 | 每轮累加 | 同 | 一致 |

## 测试覆盖（9 个）

| # | 测试 | Mock LLM | 断言 |
|---|---|---|---|
| 1 | `test_direct_text_response` | 一次返回文本 | `stop_reason="stop"`, 无工具 |
| 2 | `test_tool_call_then_text` | 工具调用 → 文本 | 工具执行, 2 轮 |
| 3 | `test_tool_result_in_messages` | 工具调用 → 文本 | tool result 消息格式正确 |
| 4 | `test_max_iterations` | 一直返回工具调用 | 超限, `stop_reason="max_iterations"` |
| 5 | `test_multiple_tool_calls` | 一次 2 个工具调用 | 两个都执行 |
| 6 | `test_tool_execution_error` | 工具返回 error | Runner 不崩溃, 继续循环 |
| 7 | `test_usage_accumulated` | 两轮不同 usage | 相加正确 |
| 8 | `test_empty_tools` | 无工具注册 | 正常返回 |
| 9 | `test_assistant_message_format` | 工具调用 | tool_calls 格式符合 OpenAI 规范 |

## 暴露的问题

1. **流式输出来自底层 chat_with_retry** — 用户等到整个工具循环结束才看到回复，不支持逐 token 显示
2. **无上下文管理** — messages 线性增长，大 context 时浪费 token
3. **无工具调用校验** — LLM 可能传错参数，直接传给 execute 了（后续加 json schema 校验）
4. **循环不设防** — LLM 可以无限循环（但 max_iterations 兜底）

## 下一 Step 方向

**Step 6：ContextBuilder** — 读取 AGENTS.md / SOUL.md / USER.md 组装系统提示，`build_system_prompt()` → `build_messages()` → `[system, *history, user]`。
