# Step 0 — 裸 HTTP 调用

## 目标

用最原始的 `urllib` 发送一个 HTTP POST 请求到 OpenAI-compatible API，打印 LLM 的回复。不引入任何外部依赖，不创建任何抽象类。

## 文件结构

```
step0/
├── main.py       # 入口：接收命令行参数 → 调 API → 打印结果
├── test_main.py  # 单元测试（mock HTTP，不依赖真实 API）
└── step0.md      # 本文件
```

## 核心函数

### `call_llm(message: str) -> dict`
- 从环境变量读取配置（`OPENAI_API_KEY`、`OPENAI_API_BASE`、`OPENAI_MODEL`）
- 构造 `POST /v1/chat/completions` 请求体（JSON）
- 用 `urllib.request.urlopen` 发送请求
- 返回解析后的 JSON dict

### `main()`
- 读取 `sys.argv[1]` 作为用户消息
- 调用 `call_llm()`，打印 `finish_reason`、token 用量、回复内容

## 暴露的问题

1. 同步阻塞 —— 不能并发处理多个请求
2. 无抽象 —— 每次调用都得手动构造 URL、header、body
3. 硬编码角色 —— 只支持 `user` 消息，不能传 system prompt
4. 无重试 —— 网络抖动 / 限流直接崩溃
5. 无工具支持 —— LLM 返回 tool_calls 无法处理

## 下一 Step（Step 1）要解决什么

- 引入 `LLMProvider` 抽象基类和 `OpenAICompatProvider` 实现
- 改为 `asyncio` + `httpx` 异步调用
- 用 `LLMResponse` dataclass 统一返回值格式
- 支持 system / user / assistant 多角色消息
