# Step 2 — 流式传输

## 目标

给 `LLMProvider` 增加 `chat_stream()` 抽象方法，`OpenAICompatProvider` 实现真正的 SSE 流式，CLI 通过 `--stream` 开关逐 token 显示。

## 文件结构

```
step2/
├── llm.py    # + chat_stream() ABC + OpenAICompatProvider 流式实现
├── main.py   # + --stream 参数
├── test.py   # 流式 mock 测试（7 个）
└── step2.md  # 本文档
```

## 核心变更

### `LLMProvider.chat_stream()`（非抽象默认方法）
- 签名与 `chat()` 一致，额外加 `on_content_delta` 回调
- 默认回退：调用 `chat()` 后把完整内容作为单次 delta 发送
- 子类可覆盖获得真正的 SSE 流式

### `OpenAICompatProvider.chat_stream()`
- `stream=True` + `stream_options={"include_usage": True}` 调用 SDK
- 逐 chunk 提取 `delta.content` 回调 `on_content_delta`
- `_assemble_from_chunks()` 将 chunk 列表重建为 `LLMResponse`
  - content：拼接所有 delta
  - tool_calls：累积同名 id 的 arguments delta
  - usage：取最后一个 chunk（SDK 在末尾发送）
- 空闲超时 30s → `finish_reason="error"`

### `main.py`
- `--stream` 参数启用流式模式
- `on_content_delta` 回调：`print(text, end="", flush=True)`
- 非流式行为与 step1 完全一致

## 与 step1 对比

| 维度 | step1 | step2 |
|---|---|---|
| Provider 接口 | `chat()` | + `chat_stream()` |
| 调用方式 | 等完整响应 | `--stream` 逐 token 显示 |
| 工具调用解析 | 仅 `chat()` 的 `_parse_response` | 流式 + `_assemble_from_chunks` |
| 空闲超时 | 无 | 30s timeout → error |

## 暴露的问题

1. **流式无重试** — 流到一半断开会抛异常（Step 3 加 `chat_stream_with_retry`）
2. **无 thinking delta** — reasoning_content 未提取
3. **工具执行** — tool_calls 只是解析了，未实际执行（Step 5 加）

## 下一 Step 方向

Step 3：给 Provider 加 `chat_with_retry()` / `chat_stream_with_retry()` 封装，支持临时错误自动重试和退避。
