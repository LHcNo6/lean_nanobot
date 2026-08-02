# Step 20 — Channel Framework 技术方案

在 Step 19 (Session System Upgrade) 基础上，对齐 nanobot 的通道框架：BaseChannel ABC、ChannelManager、Permission 系统（is_allowed / pairing / allowFrom）、send_delta 流式投递、CLI channel 首发。

---

## 设计原则

1. **最小增量** — 只改通道层（channel/pairing/manager/cli），events.py 仅加一个字段，loop/bus 零改动
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
| **修改** | `test.py` | 新增 ~45 个测试 |

---

## 技术方案

### 1. BaseChannel（新文件 channel.py，~180 行）

移植 `nanobot/channels/base.py`：

```python
class BaseChannel(ABC):
    name: str = "base"
    display_name: str = "Base"
    send_progress: bool = True
    send_tool_hints: bool = False
    show_reasoning: bool = True

    def __init__(self, config: dict | None, bus: MessageBus):
        self.config = config or {}
        self.bus = bus
        self._running = False

    @abstractmethod
    async def start(self) -> None: ...
    @abstractmethod
    async def stop(self) -> None: ...
    @abstractmethod
    async def send(self, msg: OutboundMessage) -> None: ...

    async def send_delta(self, chat_id, delta, metadata=None, *, stream_id=None, stream_end=False, resuming=False) -> None:
        pass  # 默认 no-op，子类重写启用流式

    @property
    def supports_streaming(self) -> bool:
        return bool(self.config.get("streaming", False)) and type(self).send_delta is not BaseChannel.send_delta

    def is_allowed(self, sender_id: str) -> bool:
        # 优先级：* > allowFrom 精确匹配 > pairing is_approved > 拒绝
        allow_list = self.config.get("allow_from") or self.config.get("allowFrom") or []
        if "*" in allow_list: return True
        if str(sender_id) in allow_list: return True
        return self.pairing.is_approved(self.name, str(sender_id))  # 见 pairing 注

    async def _handle_message(self, sender_id, chat_id, content, media=None, metadata=None, session_key=None, is_dm=False):
        # 拒绝：is_dm → generate_code + send(format_pairing_reply(code), metadata={PAIRING_CODE_META_KEY: code})
        #       非 DM → logger.warning
        # 放行：meta["_wants_stream"]=True（supports_streaming 时）
        #       → bus.publish_inbound(InboundMessage(channel=self.name, sender_id, chat_id, content, media, metadata, session_key_override=session_key))

    @classmethod
    def default_config(cls) -> dict: return {"enabled": False}

    @property
    def is_running(self) -> bool: return self._running
```

**注（pairing 注入）**：`is_allowed` 需要访问 pairing store。方案：`BaseChannel.__init__(self, config, bus, pairing: PairingStore | None = None)`，默认 `PairingStore()`；测试可注入临时路径的 store。属性名 `self.pairing`。

**事件/路由常量**：`PAIRING_CODE_META_KEY = "_pairing_code"`（放 pairing.py）。

### 2. PairingStore（新文件 pairing.py，~150 行）

对齐 `nanobot/pairing/store.py` API，模块函数改为类方法，`threading.Lock` 保持（CLI 同步与 async channel 并发安全）：

```python
class PairingStore:
    _ALPHABET = string.ascii_uppercase + string.digits
    _CODE_LENGTH = 8          # 如 ABCD-EFGH
    _TTL_DEFAULT_S = 600      # 10 分钟

    def __init__(self, path: Path = Path("pairing.json")): ...
    # 内部：_load（损坏 JSON → 重置+warning）、_save（原子写：临时文件+replace）、_gc_pending

    def generate_code(self, channel, sender_id, ttl=_TTL_DEFAULT_S) -> str
    def approve_code(self, code) -> tuple[str, str] | None   # (channel, sender_id)
    def deny_code(self, code) -> bool
    def is_approved(self, channel, sender_id) -> bool
    def list_pending(self) -> list[dict]
    def revoke(self, channel, sender_id) -> bool
    def revoke_channel(self, channel) -> int
    def clear_channel(self, channel) -> dict[str, int]
    def get_approved(self, channel) -> list[str]
    def format_pairing_reply(self, code) -> str
    def handle_pairing_command(self, channel, subcommand_text) -> str  # list|approve|deny|revoke
```

JSON 结构：`{"approved": {channel: [ids]}, "pending": {code: {channel, sender_id, created_at, expires_at}}}`。

### 3. 通道发现（新文件 channels/registry.py，~40 行）

复用 step18 ToolLoader 模式（`pkgutil.iter_modules`），无 entry_points：

```python
_INTERNAL = frozenset({"base", "manager", "registry"})   # 相对 nanobot 减掉（无内部通道名冲突）
DEFAULT_ENABLED_CHANNELS = frozenset({"cli"})            # nanobot 是 websocket，learn_nano 首个通道是 cli

def discover_channel_names() -> list[str]     # pkgutil 扫描，跳过 _INTERNAL/_ 前缀/包
def load_channel_class(module_name) -> type[BaseChannel]  # 找第一个 BaseChannel 子类
```

### 4. CliChannel（新文件 channels/cli.py，~90 行）

```python
class CliChannel(BaseChannel):
    name = "cli"
    display_name = "CLI"

    def __init__(self, config=None, bus=None, pairing=None, on_command=None):
        super().__init__(config, bus, pairing)
        self.on_command = on_command          # Callable[[str], bool] | None，消费返回 True
        self._turn_done = asyncio.Event()     # 回合终响到达时 set（保持"先响应后提示"UX）

    @classmethod
    def default_config(cls): return {"enabled": True, "allow_from": ["*"], "streaming": True}
    # 本地操作者直通权限，对齐 nanobot CLI 交互模式不做权限检查

    async def start(self):
        self._running = True
        while self._running:
            text = await ainput("You: ")
            if not text: continue
            if text.lower() == "/exit":
                self.stop(); break
            if self.on_command and self.on_command(text):
                continue
            self._turn_done.clear()
            await self._handle_message("user", "default", text)   # chat_id="default" → 会话 key 不变
            await self._turn_done.wait()

    async def stop(self): self._running = False

    async def send(self, msg: OutboundMessage):
        # 打印 [stop_reason] + content + tokens（复刻 step19 main.py 输出格式）
        self._turn_done.set()

    async def send_delta(self, chat_id, delta, metadata=None, *, stream_id=None, stream_end=False, resuming=False):
        # 按 (chat_id, stream_id) 缓冲；stream_end=True 时整段打印并清缓冲
```

**ainput**：`loop.run_in_executor(None, input, prompt)`（从 step19 main.py 迁入；测试 patch）。

### 5. ChannelManager（新文件 manager.py，~250 行）

对齐 `nanobot/channels/manager.py` 最小集：

```python
class ChannelManager:
    _SEND_RETRY_DELAYS = (1, 2, 4)

    def __init__(self, config: dict | None = None, bus: MessageBus | None = None, pairing: PairingStore | None = None):
        self.config = config or {}
        self.bus = bus or MessageBus()
        self.channels: dict[str, BaseChannel] = {}
        self._channel_tasks: dict[str, asyncio.Task] = {}
        self._dispatch_task: asyncio.Task | None = None
        self._started = False
        self._init_channels()

    def _init_channels(self):
        # 候选名 = discover_channel_names() ∪ config 键（config 键可覆盖内置，对齐 nanobot extra channels）
        # enabled = section.get("enabled", name in DEFAULT_ENABLED_CHANNELS)
        # 逐个 load_channel_class → 实例化（注入 bus/pairing）→ channels[name] = channel
        # 异常 → logger.warning 降级（不崩溃）

    async def start_all(self):
        if not self.channels: return
        self._started = True
        self._dispatch_task = asyncio.create_task(self._dispatch_outbound())
        await asyncio.gather(*[self._start_channel_task(n, ch) for n, ch in self.channels.items()], return_exceptions=True)

    async def stop_all(self):
        self._started = False
        if self._dispatch_task:
            self._dispatch_task.cancel() + suppress(CancelledError) await
        for name in list(self.channels): await self._stop_channel(name)

    async def _dispatch_outbound(self):
        while True:
            try:
                msg = await asyncio.wait_for(self.bus.consume_outbound(), timeout=1.0)
                channel = self.channels.get(msg.channel)
                if channel is None:
                    logger.warning("Unknown channel: {}", msg.channel); continue
                if isinstance(msg, StreamDeltaEvent):
                    if msg.finished:
                        await channel.send_delta(msg.chat_id, "", metadata=msg.metadata, stream_end=True)
                    else:
                        await channel.send_delta(msg.chat_id, msg.content, metadata=msg.metadata)
                else:
                    await self._send_with_retry(channel, msg)
            except asyncio.TimeoutError: continue
            except asyncio.CancelledError: break

    async def _send_with_retry(self, channel, msg):
        # 指数退避 (1,2,4)，重试上限 = 3（默认）；CancelledError 直接上抛
        # 全部失败 → logger.exception 后返回（不崩溃 dispatcher）

    def get_channel(self, name) -> BaseChannel | None
    def get_status(self) -> dict            # {name: {"enabled": True, "running": ...}}
    @property
    def enabled_channels(self) -> list[str]
```

**流式消息不重试**（delta 不可重放，对齐 nanobot `_send_once` 语义）；普通消息走重试。

### 6. events.py 修改

```python
@dataclass
class InboundMessage:
    content: str
    channel: str = "cli"
    sender_id: str = ""
    chat_id: str = "default"
    timestamp: datetime = field(default_factory=datetime.now)
    session_key: str | None = None
    session_key_override: str | None = None
    media: list[str] = field(default_factory=list)   # 新增
    metadata: dict[str, Any] = field(default_factory=dict)
```

其余不动：`OutboundMessage` / `StreamDeltaEvent(finished)` 保持 step19 形态，dispatcher 内做 `finished` → `stream_end` 映射。

### 7. main.py 改造（~±60 行）

```python
async def main():
    # 组件组装（registry/provider/bus/memory/session_manager/context_builder/subagent_manager/agent_loop 不变）
    pairing = PairingStore(path=Path("pairing.json"))

    async def on_command(text: str) -> bool:
        if text.lower() == "/dream": ... run_dream + 打印; return True
        if text.lower() == "/history": ... 打印会话; return True
        if text.lower() == "/new": ... invalidate + unlink; return True
        return False

    manager = ChannelManager(
        config={"cli": {"enabled": True, "allow_from": ["*"], "streaming": True}},
        bus=bus,
        pairing=pairing,
    )
    # CliChannel 的 on_command 经 manager 注入：manager.channels["cli"].on_command = on_command（或 ChannelManager 透传）
    loop_task = asyncio.create_task(agent_loop.run())
    dream_task = asyncio.create_task(_dream_loop(agent_loop))
    try:
        await manager.start_all()          # 阻塞至 CLI /exit 退出
    finally:
        agent_loop.stop()
        loop_task.cancel(); dream_task.cancel()
        await manager.stop_all()
        for t in (loop_task, dream_task): suppress(CancelledError) await
```

**on_command 注入**：ChannelManager 构造参数 `on_command` 透传给 CliChannel；或建完 channel 后由 main 直接设置 `manager.get_channel("cli").on_command = handler`。采用前者（manager 加可选参数）更整洁。

---

## 测试计划（~45 个新增，总 ~315）

| 测试类 | 数 | 覆盖 |
|--------|----|------|
| `TestPairingStore` | 8 | generate/approve/is_approved roundtrip、TTL 过期（负 ttl）、deny、跨实例持久化重载、损坏 JSON 重置、clear_channel、handle_pairing_command（list/approve/deny/revoke） |
| `TestBaseChannel` | 10 | `is_allowed` 三级优先级（* / allowFrom 精确 / pairing）、拒绝默认、`_handle_message` 放行发布（含 `_wants_stream`）、DM 拒绝 → 配对码消息（`PAIRING_CODE_META_KEY`）+ approve 后可放行、非 DM 拒绝不发布、`supports_streaming`（config+重写）、`send_delta` no-op、`is_running` |
| `TestCliChannel` | 8 | patch ainput 驱动 start、send 打印 + `_turn_done` set、send_delta 缓冲 / stream_end 整段输出、/exit 退出、on_command 消费返回 True 不发布、空输入跳过 |
| `TestChannelManager` | 14 | config 初始化发现+enabled 过滤、默认空 config 启 cli（DEFAULT_ENABLED）、路由到正确 channel、流式 delta → send_delta 映射（finished 前后）、未知 channel 警告不崩溃、发送失败重试（注入失败 channel 验证退避次数）、成功即返回、stop_all 取消任务、get_status/enabled_channels、config 键覆盖内置名 |
| `TestStep20Integration` | 3 | 端到端：CliChannel + ChannelManager + AgentLoop（假 provider）→ 发布 → 流式 delta 累积 → 终响打印；pairing 拒绝端到端（未批准 sender 的 DM 收到配对码，approve 后放行）；on_command `/new` 回调生效 |

---

## 预估工作量

| 文件 | 新增 | 修改 | 净增行 |
|------|------|------|--------|
| `channel.py` | ~180 | — | +180 |
| `pairing.py` | ~150 | — | +150 |
| `channels/__init__.py` | ~1 | — | +1 |
| `channels/registry.py` | ~40 | — | +40 |
| `channels/cli.py` | ~90 | — | +90 |
| `manager.py` | ~250 | — | +250 |
| `events.py` | — | ~+1 | +1 |
| `main.py` | — | ~±60 | +40 |
| `test.py` | — | ~+330 | +330 |
| **总计** | | | **~1080** |

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
