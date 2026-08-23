# Step 88 API Specification

## 1. MCPTransport ABC

**文件**：`tools/mcp.py`

### 方法

| 方法 | 说明 |
|------|------|
| `async connect()` | 建立连接 |
| `async send_request(request: dict) -> dict` | 发送 JSON-RPC 请求，返回响应 |
| `async disconnect()` | 关闭连接 |
| `property is_connected -> bool` | 是否已连接 |

## 2. StdioTransport

### 构造

```python
StdioTransport(command: str, args: list[str] = None, timeout: int = 30)
```

通过 asyncio.create_subprocess_exec 启动子进程，stdin/stdout 通信。

## 3. SseTransport

### 构造

```python
SseTransport(url: str, timeout: int = 30, headers: dict = None)
```

通过 HTTP POST 发送 JSON-RPC 请求，解析 JSON 响应。
（简化版：每次请求独立 HTTP 调用，不保持长连接）

## 4. MCPClient 增强

### 构造（新增参数）

```python
MCPClient(
    command: str = None,
    args: list[str] = None,
    timeout: int = 30,
    server_name: str = "default",
    transport: MCPTransport = None,  # 新增
    max_retries: int = 3,            # 新增：自动重连次数
)
```

- 传入 transport 时使用该传输
- 未传入时默认创建 StdioTransport

### 自动重连

- 触发条件：ConnectionError、EOFError、连接断开
- 退避策略：指数退避（1s, 2s, 4s）
- 最大重试次数：max_retries（默认3）

## 5. 工具发现契约

MCP 工具仍需手动注册，不通过 ToolLoader 自动发现。
