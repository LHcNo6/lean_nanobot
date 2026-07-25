# Step 0_1 — OpenAI SDK 异步调用

## 目标
将 step0 的裸 `urllib` 替换为官方 `openai` Python SDK，改为异步 `asyncio`，支持多角色消息（system / user / assistant），并自动加载 `.env` 文件。

## 新增/改进

| 对比 step0 | step0 (裸 urllib) | step0_1 (OpenAI SDK) |
|---|---|---|
| 网络库 | `urllib`（同步） | `openai` + `httpx`（异步） |
| 消息格式 | 仅 `user` | `system` + `user` + `assistant` |
| 配置加载 | 手动 `os.environ.get` | `python-dotenv` 自动加载 `.env` |
| 入口 | `python main.py "msg"` | 加 `--system` 参数 |
| 返回值 | 裸 dict | SDK 对象 `.model_dump()` |

## 核心函数

### `call_llm(messages: list[dict], model: str | None) -> dict`
- 用 `AsyncOpenAI` 客户端调用 `chat.completions.create`
- 接收完整的 messages 列表，支持 system 消息
- 返回序列化后的完整 API 响应 dict

### `main()`
- 使用 `argparse` 解析 `--system` 和 `message`
- 调用 `asyncio.run(main())` 异步执行

## 暴露的问题

1. 仍然**没有抽象** —— 直接依赖 `AsyncOpenAI`，换 Anthropic 要重写
2. 仍然**没有工具调用** —— tool_calls 返回了但没处理
3. 仍然**没有流式** —— 等完整响应才返回
4. 安装依赖增多（`openai`, `python-dotenv`）

## 下一 Step（Step 1）要解决什么

- 引入 `LLMProvider` 抽象基类
- 用 `LLMResponse` dataclass 统一返回值
- 将 OpenAI SDK 调用封装为 `OpenAICompatProvider`
- 实现 `chat_stream` 流式传输基础
