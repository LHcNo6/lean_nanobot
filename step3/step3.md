# Step 3 — 重试逻辑

## 目标

给 `LLMProvider` 添加 `chat_with_retry()` / `chat_stream_with_retry()` 方法，自动重试临时错误并区分可重试与不可重试异常。

## 文件结构

```
step3/
├── __init__.py                 # 包标记
├── llm.py                      # LLMResponse, ToolCallRequest, RetryConfig 数据类
├── provider.py                 # LLMProvider(ABC) + 重试方法 + 异常分类
├── openai_compat_provider.py   # OpenAICompatProvider（max_retries=0）
├── main.py                     # CLI + --retry 标志
├── test.py                     # 20 个测试
└── step3.md                    # 本文档
```

## 核心设计

### 文件拆分（对齐 nanobot）

| 功能 | nanobot 路径 | 本 step 文件 |
|---|---|---|
| 数据类 | `nanobot/providers/base.py` 顶部 | `step3/llm.py` |
| Provider ABC + 重试 | `nanobot/providers/base.py` | `step3/provider.py` |
| OpenAI 实现 | `nanobot/providers/openai_compat_provider.py` | `step3/openai_compat_provider.py` |

### retry 方法

两个重试方法都在 `LLMProvider` 基类实现，子类无需修改：

```python
async def chat_with_retry(self, ..., retry_config=None) -> LLMResponse
async def chat_stream_with_retry(self, ..., retry_config=None) -> LLMResponse
```

- 调用 `self.chat()` / `self.chat_stream()` 并捕捉异常
- 可重试异常 → 指数退避 + jitter 后重试
- 不可重试 → 立即抛出
- 流式额外约束：已通过 `on_content_delta` 投递内容后不再重试

### 异常分类

```
可重试：
  asyncio.TimeoutError                — 流空闲超时
  openai.APIConnectionError           — 网络断开
  openai.APITimeoutError              — 请求超时
  openai.RateLimitError (429)         — 频率限制
  openai.InternalServerError (500+)   — 服务端错误
  openai.APIStatusError (status>=500) — 兜底

不可重试：
  openai.AuthenticationError (401)
  openai.BadRequestError (400)
  openai.PermissionDeniedError (403)
  openai.NotFoundError (404)
  openai.UnprocessableEntityError (422)
  其他通用 Exception
```

### 指数退避公式

```python
delay = min(base_delay * 2^attempt, max_delay)
delay *= 0.5 + random()   # full jitter
```

默认 `RetryConfig(max_retries=3, base_delay=1.0, max_delay=60.0)`

### 与 step2 的关键差异

| 行为 | step2 | step3 |
|---|---|---|
| SDK 重试 | 默认（openai 内部 3 次） | `max_retries=0`，仅在 `chat_with_retry` 层重试 |
| 流式超时 | 捕获并返回 error 响应 | 抛出 `TimeoutError` 由 retry 层处理 |
| Provider 调用 | 直接 `chat()` / `chat_stream()` | + `chat_with_retry()` / `chat_stream_with_retry()` |
| CLI | `--stream` | + `--retry` |
| 文件组织 | 单 `llm.py` | 拆为 `llm.py` + `provider.py` + `openai_compat_provider.py` |

## 测试覆盖

**单元测试（8 个）**：`_is_retryable_exception` 对所有异常类型正确分类。

**`chat_with_retry`（4 个）**：
| 测试 | 场景 |
|---|---|
| `test_success_first_try` | 一次成功，无重试 |
| `test_fail_then_succeed` | 首次失败重试，第二次成功 |
| `test_exhausted` | 耗尽重试次数后抛出 |
| `test_non_retryable_propagates` | 不可重试异常立即抛出 |

**`chat_stream_with_retry`（5 个）**：
| 测试 | 场景 |
|---|---|
| `test_stream_success_first_try` | 一次成功 |
| `test_stream_fail_before_delta` | 错误发生在第一个 chunk 前，重试成功 |
| `test_stream_fail_after_delta_no_retry` | 已投递 delta 后错误，不重试 |
| `test_stream_exhausted` | 耗尽后抛出 |
| `test_stream_timeout_before_delta_retried` | 超时在第一个 chunk 前，重试成功 |

**集成测试（3 个）**：Mock 真实 OpenAI SDK，验证 `chat_with_retry` / `chat_stream_with_retry` 完整链路。

## 暴露的问题

1. **流式重试的 at-most-once 语义** — 如果错误发生在 `on_content_delta` 刚投递后，我们放弃重试，调用方需自行处理不完整输出
2. **无持久重试模式** — 没有 nanobot 的 `"persistent"` 模式（无限重试直到成功或达到相同错误次数上限）
3. **`RetryConfig` 无持久化** — 代码内构造，暂无配置加载
4. **流式超时无区分** — 空闲 30s 超时 vs 首 chunk 慢都当作 `TimeoutError` 处理

## 下一 Step 方向

**Step 4：Tool 基类 + Registry** — 定义 `Tool(ABC)` 和 `ToolRegistry`，实现 `echo_tool`，为 AgentRunner 做好准备。
