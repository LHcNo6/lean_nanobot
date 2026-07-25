# Step 9 — 异步消息总线（MessageBus）

## 目标

将消息的生产者（CLI 输入）与消费者（Agent 处理管道）通过 **异步消息总线** 解耦，为后续步骤引入多通道、AgentLoop 状态机做准备。

## 新增文件

| 文件 | 行数 | 职责 |
|------|------|------|
| `events.py` | 22 | `InboundMessage` / `OutboundMessage` 数据类 |
| `bus.py` | 26 | `MessageBus` — 两个 `asyncio.Queue`（inbound/outbound） |

## 修改文件

| 文件 | 变化 |
|------|------|
| `main.py` | 完全重写为 **总线驱动** 架构 |
| `test.py` | 增加 9 个总线测试，共 30 个测试 |

## 设计

### 数据流对比

**Before (step8)：**
```
main() 循环:
  输入 → SessionManager → Consolidator → ContextBuilder → AgentRunner → 输出
  全部串行，一个线程
```

**After (step9)：**
```
┌─ main() 前台 ─────────────────────┐
│  输入 → bus.publish_inbound()      │
│  bus.consume_outbound() → 输出     │
└──────────┬──────────────┬──────────┘
           │ inbound      │ outbound
           ▼              ▲
    ┌──── MessageBus ──────┐
    │  inbound: Queue       │
    │  outbound: Queue      │
    └────┬──────────────┬───┘
         │ consume      │ publish
         ▼              │
┌─ _agent_loop (bg) ────┘──────────┐
│  SessionManager → Consolidator    │
│  ContextBuilder → AgentRunner     │
│  bus.publish_outbound()           │
└───────────────────────────────────┘
```

### 事件数据类

```python
@dataclass
class InboundMessage:
    content: str
    channel: str = "cli"            # 来源通道
    sender_id: str = ""             # 用户标识
    chat_id: str = "default"        # 会话标识
    timestamp: datetime = ...       # 自动生成
    session_key: str | None = None  # session 覆盖
    metadata: dict = {}             # 命令/控制信息

@dataclass
class OutboundMessage:
    content: str
    channel: str = "cli"
    chat_id: str = "default"
    metadata: dict = {}             # stop_reason/tokens 等信息
```

### MessageBus API

| 方法 | 说明 |
|------|------|
| `publish_inbound(msg)` | 向 inbound 队列投递消息 |
| `consume_inbound()` | 阻塞获取下一条入站消息 |
| `publish_outbound(msg)` | 向 outbound 队列投递响应 |
| `consume_outbound()` | 阻塞获取下一条出站消息 |
| `inbound_size` / `outbound_size` | 队列当前长度（属性） |

## 核心改动：main.py

### 代理任务（_agent_loop）

后台 `asyncio.Task`，循环调用 `bus.consume_inbound()`，对每条消息：

1. 解析 metadata 中的 `command`（`/exit`、`/history`、`/new`）
2. `SessionManager.get_or_create()` 获取/创建会话
3. `Consolidator.maybe_consolidate()` 检查是否需要压缩
4. `Session.get_history(max_tokens=budget)` 获取 token 预算内的历史
5. `ContextBuilder.build_messages()` 组装 system + history + user
6. `AgentRunner.run()` 执行 LLM 调用 + 工具
7. `Session.import_messages()` + `SessionManager.save()` 持久化
8. `bus.publish_outbound()` 返回结果

### 前台主循环

```python
bus = MessageBus()
agent = asyncio.create_task(_agent_loop(bus, ...))

while True:
    text = await ainput("You: ")
    await bus.publish_inbound(InboundMessage(content=text))
    resp = await bus.consume_outbound()
    print(resp.content)
```

命令通过 `metadata["command"]` 传递，代理任务响应后返回带对应 metadata 的 outbound 消息。

## 测试

9 个新增测试：

| 测试 | 内容 |
|------|------|
| `test_publish_consume_inbound` | 发布并消费 InboundMessage |
| `test_publish_consume_outbound` | 发布并消费 OutboundMessage |
| `test_multiple_messages_fifo` | 5 条消息 FIFO 顺序 |
| `test_inbound_size` | inbound 队列长度跟踪 |
| `test_outbound_size` | outbound 队列长度跟踪 |
| `test_inbound_message_fields` | InboundMessage 各字段 |
| `test_outbound_message_fields` | OutboundMessage 各字段 |
| `test_concurrent_producer_consumer` | 100 条消息并发无丢失 |
| `test_agent_roundtrip` | 完整总线往返（inbound → 处理 → outbound） |

## 关键决策

| 决策 | 选择 | 原因 |
|------|------|------|
| Queue 是否 bounded | 否（unbounded） | 对齐 nanobot，当前无反压需求 |
| 命令处理方式 | metadata["command"] | 不需要额外机制，代理任务检查即可 |
| Session/chat 路由 | session_key 覆盖或 chat_id | 总线层感知会话概念 |
| agent 取消策略 | main 退出时 `agent.cancel()` | 干净关闭 |

## 下一站

Step 10 — AgentLoop 状态机：将 `_agent_loop` 重构为正式的状态机（RESTORE → BUILD → RUN → SAVE → RESPOND → DONE）。
