# Step 1 — Provider 抽象抽象基类 + OpenAI 实现

## 目标

将 step0_1 中直接调用 OpenAI SDK 的 `call_llm()` 函数重构为 **Provider 抽象基类 + 具体实现** 模式，统一返回值 `LLMResponse`，为后续支持多 Provider 和工具执行打下基础。

## 文件结构

```
step1/
├── llm.py    # LLMProvider(ABC)、LLMResponse、ToolCallRequest、OpenAICompatProvider
├── main.py   # CLI 入口
├── test.py   # 单元测试（mock SDK，不依赖真实 API）
└── step1.md  # 本文档
```

## 核心设计

### `LLMResponse` dataclass
```python
content: str | None          # 回复文本（tool_calls 时可能为 None）
tool_calls: list[ToolCallRequest]  # 工具调用请求
finish_reason: str           # "stop" | "tool_calls" | "length" | "error"
usage: dict[str, int]        # prompt/completion tokens
```

### `LLMProvider` (ABC)
```python
async def chat(self, messages, tools=None, model=None, ...) -> LLMResponse
```
抽象方法，所有 provider 实现此接口。

### `OpenAICompatProvider(LLMProvider)`
- 构造时接收 `api_key`, `api_base`, `model`
- `chat()` 内部调用 `AsyncOpenAI.chat.completions.create()`
- 将 SDK 响应转换为 `LLMResponse`
- `from_env()` 类方法从 `.env` 自动创建

## 与 step0_1 的对比

| 维度 | step0_1 | step1 |
|---|---|---|
| 接口 | 裸函数 `call_llm()` | `LLMProvider.chat()` 抽象方法 |
| 返回值 | 裸 dict | 结构化 `LLMResponse` |
| 扩展性 | 换 Provider 需重写 | 加新类实现 ABC |
| 工具调用 | 忽略 | 解析为 `ToolCallRequest` 结构 |

## 暴露的问题

1. **没有流式** — `chat()` 等完整响应才返回
2. **没有重试** — 网络错误直接抛异常
3. **没有工具执行** — `ToolCallRequest` 仅在返回值中解析，未实际执行
4. **单 provider** — 不支持 fallback / 切换

## 下一 Step 方向

Step 2：给 `LLMProvider` 加 `chat_stream()` 方法，`OpenAICompatProvider` 实现真正的 SSE 流式，CLI 逐 token 显示。
