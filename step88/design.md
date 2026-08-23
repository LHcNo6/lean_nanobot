# Step 88 Design: MCP SSE 传输 + 自动重连

## 1. 架构

```
tools/mcp.py（修改）
  ├── MCPTransport(ABC)           传输层抽象
  │   ├── connect()               建立连接
  │   ├── send_request(req)       发送 JSON-RPC 请求并等待响应
  │   └── disconnect()            关闭连接
  ├── StdioTransport              stdio 子进程传输（从 MCPClient 提取）
  ├── SseTransport                SSE HTTP 传输（新增）
  ├── MCPClient                   接受 transport 参数
  └── 自动重连逻辑                 在 send_request 失败时重连
```

## 2. MCPTransport ABC

```python
class MCPTransport(ABC):
    @abstractmethod
    async def connect(self) -> None: ...
    @abstractmethod
    async def send_request(self, request: dict) -> dict: ...
    @abstractmethod
    async def disconnect(self) -> None: ...
    @property
    @abstractmethod
    def is_connected(self) -> bool: ...
```

## 3. SseTransport

SSE 传输协议：
- 连接：HTTP GET {url}，Header `Accept: text/event-stream`
- 发送请求：HTTP POST {url}，Body 为 JSON-RPC 请求
- 接收响应：从 SSE 流中解析 `data: {...}` 事件
- 简化：每次请求建立新的 SSE 连接（长连接复杂，最小增量用短连接）

实际上 MCP SSE 协议更复杂，简化版用：
- POST 请求到 endpoint，等待 JSON 响应（类 RPC over HTTP）
- 这是 SSE 传输的最小可行实现

## 4. 自动重连

在 MCPClient._send_request 中：
1. 尝试发送请求
2. 如果连接断开（ConnectionError/EOF），触发重连
3. 指数退避：1s, 2s, 4s
4. 最多重试 3 次
5. 重连成功后重新发送请求

## 5. 向后兼容

MCPClient 默认使用 StdioTransport，保持原有接口不变。
新增 `transport` 参数允许传入自定义传输。

## 6. 测试策略

- MCPTransport ABC 定义
- StdioTransport 功能（mock subprocess）
- SseTransport 连接/发送（mock urllib）
- MCPClient 接受 transport 参数
- 自动重连触发（模拟连接断开）
- 重连指数退避
