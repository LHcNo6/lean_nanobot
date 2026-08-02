# Step 20 — Channel Framework

在 Step 19 (Session System Upgrade) 基础上，对齐 nanobot 的通道框架：BaseChannel ABC、ChannelManager、Permission 系统（is_allowed / pairing / allowFrom）、send_delta 流式投递、CLI channel 首发。

---

## 设计原则

1. **最小增量** — 只改通道层（channel/pairing/manager/cli），loop/bus 零改动，events.py 仅加一个字段
2. **别名对齐** — 类/方法名与 nanobot 一致（`BaseChannel`、`ChannelManager`、`is_allowed`、`_handle_message`、`send_delta`、`PairingStore`），import 路径 `step19.` → `step20.`
3. **无 config 系统**（step22 才有 pydantic）— 通道配置用普通 dict：`config = {"cli": {"enabled": True, "allow_from": ["*"], "streaming": True}}`
4. **用户决策**（已确认）：
   - Pairing store 用类 + 可注入路径（`class PairingStore(path: Path)`），默认 `./pairing.json`
   - CLI 命令用 `on_command: Callable[[str], bool]` 回调（main.py 提供），`/exit` 由 channel 原生处理
   - Dispatcher 最小范围：路由 + 重试(1,2,4) + 流分发；不做流合并/重复抑制/progress 门控

---

## 文件变更总览

| 操作 | 文件 | 说明 |
|------|------|------|
| **新增** | `channel.py` | `BaseChannel` ABC（移植 nanobot base.py 最小集） |
| **新增** | `pairing.py` | `PairingStore` 类（对齐 nanobot pairing/store.py API） |
| **新增** | `channels/__init__.py` | 空包（pkgutil 扫描目标） |
| **新增** | `channels/registry.py` | 通道自动发现（复用 step18 ToolLoader 的 pkgutil 模式） |
| **新增** | `channels/cli.py` | `CliChannel` 第一个实现 |
| **新增** | `manager.py` | `ChannelManager` |
| **修改** | `events.py` | `InboundMessage` 加 `media: list[str]` 字段（默认空，向后兼容） |
| **修改** | `main.py` | REPL 迁入 CliChannel，改为 ChannelManager 组装启动 |
| **修改** | `test.py` | 新增 48 个测试 |

---

## 技术方案

### 1. BaseChannel（新文件 channel.py）

移植 `nanobot/channels/base.py`：

```python
class BaseChannel(ABC):
    name: str = "base"
    display_name: str = "Base"
    send_progress: bool = True
    send_tool_hints: bool = False
    show_reasoning: bool = True

    def __init__(self, config: dict | None = None, bus: MessageBus | None = None,
                 pairing: PairingStore | None = None):
        self.config = config or {}
        self.bus = bus
        self.pairing = pairing or PairingStore()
        self._running = False

    @abstractmethod
    async def start(self) -> None: ...
    @abstractmethod
    async def stop(self) -> None: ...
    @abstractmethod
    async def send(self, msg: OutboundMessage) -> None: ...

    async def send_delta(self, chat_id, delta, metadata=None, *, stream_id=None,
                         stream_end=False, resuming=False) -> None:
        return  # 默认 no-op，子类重写启用流式

    @property
    def supports_streaming(self) -> bool:
        # config.streaming 且重写了 send_delta
        return bool(self.config.get("streaming", False)) and type(self).send_delta is not BaseChannel.send_delta

    def is_allowed(self, sender_id: str) -> bool:
        # 优先级：allowFrom "*" > 精确匹配 > pairing is_approved > 拒绝
        allow_list = self.config.get("allow_from") or self.config.get("allowFrom") or []
        if "*" in allow_list: return True
        if str(sender_id) in allow_list: return True
        return self.pairing.is_approved(self.name, str(sender_id))

    async def _handle_message(self, sender_id, chat_id, content, media=None,
                              metadata=None, session_key=None, is_dm=False):
        # 拒绝：is_dm → generate_code + send(format_pairing_reply(code),
        #        metadata={PAIRING_CODE_META_KEY: code})；非 DM → 打印警告
        # 放行：supports_streaming 时 meta["_wants_stream"]=True
        #       → bus.publish_inbound(InboundMessage(channel/name, sender_id, chat_id,
        #                                            content, media, metadata, session_key_override))

    @classmethod
    def default_config(cls) -> dict: return {"enabled": False}

    @property
    def is_running(self) -> bool: return self._running
```

### 2. PairingStore（新文件 pairing.py）

对齐 `nanobot/pairing/store.py` API，模块函数改为类方法，`threading.Lock` 保持（CLI 同步与 async channel 并发安全）：

```python
class PairingStore:
    _ALPHABET = string.ascii_uppercase + string.digits
    _CODE_LENGTH = 8          # 如 ABCD-EFGH
    _TTL_DEFAULT_S = 600      # 10 分钟

    def __init__(self, path: Path = Path("pairing.json")): ...
    # _load（损坏 JSON → 重置+警告）/ _save（临时文件 + fsync + os.replace 原子写）/ _gc_pending

    def generate_code(self, channel, sender_id, ttl=_TTL_DEFAULT_S) -> str
    def approve_code(self, code) -> tuple[str, str] | None   # (channel, sender_id)
    def deny_code(self, code) -> bool
    def is_approved(self, channel, sender_id) -> bool
    def list_pending(self) -> list[dict]
    def revoke(self, channel, sender_id) -> bool
    def revoke_channel(self, channel) -> int
    def clear_channel(self, channel) -> dict[str, int]
    def get_approved(self, channel) -> list[str]
    @staticmethod
    def format_pairing_reply(code) -> str
    def handle_pairing_command(self, channel, subcommand_text) -> str  # list|approve|deny|revoke
```

JSON 结构：`{"approved": {channel: [ids]}, "pending": {code: {channel, sender_id, created_at, expires_at}}}`。
模块常量 `PAIRING_CODE_META_KEY = "_pairing_code"`。

### 3. 通道发现（新文件 channels/registry.py）

复用 step18 ToolLoader 模式（`pkgutil.iter_modules`），无 entry_points：

```python
_INTERNAL = frozenset({"base", "manager", "registry"})
DEFAULT_ENABLED_CHANNELS = frozenset({"cli"})   # nanobot 是 websocket，learn_nano 首个通道是 cli

def discover_channel_names() -> list[str]      # pkgutil 扫描，跳过 _INTERNAL/_ 前缀/包
def load_channel_class(module_name) -> type[BaseChannel]   # 找第一个 BaseChannel 子类
```

### 4. CliChannel（新文件 channels/cli.py）

```python
class CliChannel(BaseChannel):
    name = "cli"
    display_name = "CLI"

    def __init__(self, config=None, bus=None, pairing=None,
                 on_command: Callable[[str], bool] | None = None,
                 chat_id: str = "default"):
        super().__init__(config, bus, pairing)
        self.on_command = on_command
        self.chat_id = chat_id
        self._turn_done = asyncio.Event()
        self._buffers: dict[tuple[str, str], list[str]] = {}

    @classmethod
    def default_config(cls): return {"enabled": True, "allow_from": ["*"], "streaming": True}
    # 本地操作者直通权限，对齐 nanobot CLI 交互模式不做权限检查

    async def start(self):
        self._running = True
        while self._running:
            text = await ainput("You: ")
            if not text: continue
            if text.lower() == "/exit":
                await self.stop(); break
            if self.on_command and await self.on_command(text): continue
            self._turn_done.clear()
            await self._handle_message("user", self.chat_id, text)
            await self._turn_done.wait()

    async def send(self, msg: OutboundMessage):
        # 打印 [stop_reason] + content + tokens（复刻 step19 main.py 输出格式）
        self._turn_done.set()

    async def send_delta(self, chat_id, delta, metadata=None, *, stream_id=None,
                         stream_end=False, resuming=False):
        # 按 (chat_id, stream_id) 缓冲；stream_end=True 时整段打印并清缓冲
```

- `ainput`：`loop.run_in_executor(None, input, prompt)`（从 step19 main.py 迁入；测试 mock）
- `_turn_done`：保持"先响应后提示"UX（对齐 nanobot 交互模式的 turn_done Event 语义）

### 5. ChannelManager（新文件 manager.py）

对齐 `nanobot/channels/manager.py` 最小集：

```python
class ChannelManager:
    _SEND_RETRY_DELAYS = (1, 2, 4)
    _SEND_MAX_RETRIES = 3

    def __init__(self, config: dict | None = None, bus: MessageBus | None = None,
                 pairing: PairingStore | None = None,
                 on_command: Callable[[str], bool] | None = None):
        self.config = config or {}
        self.bus = bus or MessageBus()
        self.pairing = pairing or PairingStore()
        self.on_command = on_command
        self.channels: dict[str, BaseChannel] = {}
        self._channel_tasks: dict[str, asyncio.Task] = {}
        self._dispatch_task: asyncio.Task | None = None
        self._started = False
        self._init_channels()

    def _init_channels(self):
        # 候选名 = discover_channel_names() ∪ config 键（对齐 nanobot extra channels）
        # enabled = section.get("enabled", name in DEFAULT_ENABLED_CHANNELS)
        # 逐个 load_channel_class → 实例化（注入 bus/pairing）→ channels[name]
        # 未知名/导入失败/初始化异常 → 打印警告降级（不崩溃）
        # on_command 注入：hasattr(channel, "on_command") 时赋值

    async def start_all(self):
        # dispatcher 任务 + 各 channel 任务 gather（channel 退出即返回）
    async def stop_all(self):
        # 取消 dispatcher（suppress CancelledError）→ 逐个 stop channel

    async def _dispatch_outbound(self):
        # wait_for(consume_outbound, 1.0) 轮询
        # StreamDeltaEvent(finished=False) → send_delta(delta)
        # StreamDeltaEvent(finished=True)  → send_delta("", stream_end=True)
        # 普通 OutboundMessage → _send_with_retry
        # 未知 channel → 警告后 continue；TimeoutError → continue；CancelledError → break

    async def _send_with_retry(self, channel, msg):
        # 指数退避 (1,2,4)，上限 3 次；CancelledError 上抛；全失败打印后返回

    def get_channel(self, name) / get_status(self) / enabled_channels
```

**流式消息不重试**（delta 不可重放，对齐 nanobot `_send_once` 语义）；普通消息走重试。

### 6. events.py 修改

```python
@dataclass
class InboundMessage:
    ...
    session_key_override: str | None = None
    media: list[str] = field(default_factory=list)   # 新增
    metadata: dict[str, Any] = field(default_factory=dict)
```

其余不动：`OutboundMessage` / `StreamDeltaEvent(finished)` 保持 step19 形态，dispatcher 内做 `finished` → `stream_end` 映射。

### 7. main.py 改造

- REPL 循环迁入 CliChannel.start()（`/exit` 原生）；`/dream` `/history` `/new` 迁入 `on_command` 回调（async，消费返回 True）
- 组件组装不变 → `ChannelManager(config={"cli": {...}}, bus, pairing, on_command)` → `await manager.start_all()` 阻塞至 `/exit` → finally 停止 agent/dream 任务 + `manager.stop_all()`
- `manager.get_channel("cli").chat_id = session_key`（保持 step19 会话 key 语义）

---

## 测试计划（48 个新增，总 318）

| 测试类 | 数 | 覆盖 |
|--------|----|------|
| `TestPairingStore` | 9 | generate/approve roundtrip、TTL 过期、deny、跨实例持久化、损坏 JSON 重置、clear_channel、revoke/revoke_channel、handle_pairing_command（list/approve/deny/revoke）、format_pairing_reply |
| `TestBaseChannel` | 11 | `is_allowed` 三级优先级（* / allowFrom / pairing / 拒绝）、`_handle_message` 放行发布（media/session_key_override 透传）、`_wants_stream` 标记、DM 拒绝 → 配对码（含 approve 后放行）、非 DM 拒绝静默、`supports_streaming`、生命周期、`send_delta` no-op |
| `TestCliChannel` | 9 | patch ainput 驱动 start（发布+退出）、send 打印 + `_turn_done`、send_delta 缓冲/stream_end/stream_id 隔离、/exit、空输入跳过、on_command 消费、default_config |
| `TestChannelManager` | 16 | 发现+enabled 过滤、默认启 cli、未知名跳过、section 透传、on_command 注入、get_status、路由（普通/流式 delta/stream_end/未知 channel）、重试成功/耗尽、start_all/stop_all 生命周期、无 channel no-op、幂等 |
| `TestStep20Integration` | 3 | 端到端 CLI 回合（CliChannel+Manager+AgentLoop 假 provider → 流式累积+终响打印+会话落盘）、pairing 拒绝端到端（DM 配对码 → approve → 放行回复）、on_command `/new` 端到端 |

---

## 预估工作量

| 文件 | 新增 | 修改 | 净增行 |
|------|------|------|--------|
| `channel.py` | ~130 | — | +130 |
| `pairing.py` | ~230 | — | +230 |
| `channels/__init__.py` | ~1 | — | +1 |
| `channels/registry.py` | ~35 | — | +35 |
| `channels/cli.py` | ~90 | — | +90 |
| `manager.py` | ~200 | — | +200 |
| `events.py` | — | ~+1 | +1 |
| `main.py` | — | ~±60 | +20 |
| `test.py` | — | ~+470 | +470 |
| **总计** | | | **~1180** |

---

## 不做事项（推迟到后续步骤）

| 功能 | 原因 | 计划步骤 |
|------|------|----------|
| entry_points 插件发现 / feishu 多实例 / websocket channel | 无消费方 | 后续 |
| outbound_events 事件族（ProgressEvent/RetryWaitEvent/TurnEndEvent） | step19 无对应运行态语义 | 视需求 |
| 流式 delta 合并（`_coalesce_stream_deltas`）/ 重复抑制 / progress 门控 | CLI 无实际收益 | 视需求 |
| pydantic `ChannelsConfig`（`send_max_retries`、`send_progress` 等） | 无 config 系统 | step22 |
| CommandRouter（`/pairing` 等走 agent） | 无命令路由器 | 后续 |
| `send_progress`/`send_tool_hints`/`show_reasoning` 运行时门控 | 无 progress 事件源 | 视需求 |
