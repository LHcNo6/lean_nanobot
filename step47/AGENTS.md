# Step 45 — `_save_turn` 增强 + `_state_command` 持久化（最小增量版）

> 本文档为 step45 的原理分析与实现指南，遵循 `learn_nano/AGENTS.md` 开发原则（最小增量）。
> 对齐基准：`nananobot/nanobot/agent/loop.py`。
> 上游：step44（StateTraceEntry）。
> 下游：step46（runner malformed_retry）。

---

## 一、这一阶段解决什么问题

### 1.1 问题背景

step44 的持久化层有两个对齐缺口：

1. **`_save_turn` 不完整**：
   - 不弹出消息中的 `_meta` 字段（内部元数据会泄漏到持久化历史）
   - 不处理 `RUNTIME_CONTEXT_MESSAGE_META`（运行时上下文元数据无法正确持久化）
   - `_sanitize_persisted_blocks` 不处理 `image_url` data: 块（base64 图片会写入历史，占用空间且无法回放）
   - `updated_at` 存 ISO 字符串而非 datetime 对象（与 nanobot 类型不一致）

2. **`_state_command` 不持久化**：
   - shortcut 命令（如 /help、/status）直接返回 outbound，不写入 session 历史
   - nanobot 会持久化 user+assistant 消息（带 `_command` 标记），使 WebUI 等前端能看到命令历史

### 1.2 为什么需要

| 问题 | 影响 |
|------|------|
| `_meta` 泄漏 | 内部元数据（如 runtime_context）写入历史，可能干扰 LLM 上下文 |
| image_url data: 持久化 | base64 图片占用大量空间，且历史回放时无法正确处理 |
| updated_at 类型不一致 | Session 序列化/反序列化可能出问题，跨版本兼容性风险 |
| shortcut 不持久化 | 命令历史不可见，WebUI 刷新后丢失命令记录 |

---

## 二、nanobot 源码对齐分析

### 2.1 `_save_turn` 中的 `_meta` 处理（nanobot loop.py）

```python
for m in messages[skip:]:
    entry = dict(m)
    internal_meta = entry.pop("_meta", None)  # 1. 弹出 _meta
    runtime_context_meta = (
        internal_meta.get(RUNTIME_CONTEXT_MESSAGE_META)
        if isinstance(internal_meta, dict) else None
    )
    role, content = entry.get("role"), entry.get("content")
    # ... 空 assistant 跳过、tool 结果处理 ...
    elif role == "user":
        if isinstance(content, list):
            filtered = self._sanitize_persisted_blocks(content)
            if not filtered:
                continue
            entry["content"] = filtered
        if isinstance(runtime_context_meta, dict):
            entry[RUNTIME_CONTEXT_HISTORY_META] = runtime_context_meta  # 2. 设置历史元数据
    entry.setdefault("timestamp", datetime.now().isoformat())
    session.messages.append(entry)
# ...
session.updated_at = datetime.now()  # 3. datetime 对象，不是字符串
```

### 2.2 `_sanitize_persisted_blocks` 中的 image_url 处理（nanobot loop.py）

```python
def _sanitize_persisted_blocks(self, content, *, should_truncate_text=False):
    for block in content:
        # ...
        if block.get("type") == "image_url" and block.get("image_url", {}).get(
            "url", ""
        ).startswith("data:image/"):
            path = (block.get("_meta") or {}).get("path", "")
            filtered.append({"type": "text", "text": image_placeholder_text(path)})
            continue
        # ... text truncate ...
```

`image_placeholder_text(path)` 生成类似 `[image: path]` 的占位文本，替代 base64 data: URL。

### 2.3 `_state_command` shortcut 持久化（nanobot loop.py）

```python
async def _state_command(self, ctx):
    raw = ctx.msg.content.strip()
    # ... is_user_turn 判定 ...
    result = await self.commands.dispatch(cmd_ctx)
    if result is not None:
        ctx.outbound = result
        if cmd_ctx.raw.lower() != "/new":  # /new 清空会话，不持久化
            ctx.user_persisted_early = self._persist_user_message_early(
                ctx.msg, ctx.session, _command=True  # 持久化 user，带 _command 标记
            )
            ctx.session.add_message(
                "assistant", result.content, _command=True  # 持久化 assistant
            )
            self.sessions.save(ctx.session)
            self._clear_pending_user_turn(ctx.session)
        return "shortcut"
    return "dispatch"
```

**关键点**：
- `_command=True` 标记命令消息，`get_history` 可过滤掉命令消息（不进入 LLM 上下文）
- `/new` 命令排除（因为它会清空会话）
- 持久化后调用 `sessions.save` 和 `_clear_pending_user_turn`

---

## 三、step44 现状与缺口

### 3.1 `_save_turn` 现状

```python
for m in messages[skip:]:
    entry = dict(m)
    role, content = entry.get("role"), entry.get("content")  # ❌ 不弹出 _meta
    # ... 空 assistant 跳过、tool 结果处理 ...
    elif role == "user":
        if isinstance(content, list):
            filtered = self._sanitize_persisted_blocks(content)
            # ❌ 不处理 RUNTIME_CONTEXT_MESSAGE_META
            if not filtered:
                continue
            entry["content"] = filtered
    entry.setdefault("timestamp", datetime.now().isoformat())
    session.messages.append(entry)
# ...
session.updated_at = datetime.now().isoformat()  # ❌ 字符串，不是对象
```

### 3.2 `_sanitize_persisted_blocks` 现状

```python
def _sanitize_persisted_blocks(self, content, *, should_truncate_text=False):
    for block in content:
        # ❌ 不处理 image_url data: 块
        if block.get("type") == "text" and should_truncate_text and ...:
            # text truncate
            continue
        filtered.append(block)
```

### 3.3 `_state_command` 现状

```python
async def _state_command(self, ctx):
    raw = ctx.msg.content.strip()
    if not raw.startswith("/"):
        return "dispatch"
    cmd_ctx = CommandContext(...)
    result = await self.commands.dispatch(cmd_ctx)
    if result is None:
        return "dispatch"
    result.channel = ctx.msg.channel
    result.chat_id = ctx.msg.chat_id
    ctx.outbound = result
    return "shortcut"  # ❌ 不持久化
```

### 3.4 已有基础设施（好消息）

- ✅ `_persist_user_message_early` 已支持 `**kwargs`（可传 `_command=True`）
- ✅ `RUNTIME_CONTEXT_MESSAGE_META` / `RUNTIME_CONTEXT_HISTORY_META` 常量已存在
- ✅ `session.add_message` 支持 `**kwargs`（可传 `_command=True`）
- ✅ `_clear_pending_user_turn` 方法已存在

---

## 四、具体实现思路（最小增量，2 个部分）

### 第一部分：`_save_turn` 增强（3 个改动点）

#### 改动 1：`_meta` 弹出 + RUNTIME_CONTEXT_MESSAGE_META 处理

在 `_save_turn` 循环开头添加：

```python
for m in messages[skip:]:
    entry = dict(m)
    # step45：弹出内部 _meta，提取 runtime_context 元数据
    internal_meta = entry.pop("_meta", None)
    runtime_context_meta = (
        internal_meta.get(RUNTIME_CONTEXT_MESSAGE_META)
        if isinstance(internal_meta, dict) else None
    )
    role, content = entry.get("role"), entry.get("content")
    # ...
    elif role == "user":
        if isinstance(content, list):
            filtered = self._sanitize_persisted_blocks(content)
            if not filtered:
                continue
            entry["content"] = filtered
        # step45：将 runtime_context 元数据设置到历史消息
        if isinstance(runtime_context_meta, dict):
            entry[RUNTIME_CONTEXT_HISTORY_META] = runtime_context_meta
```

需要在 loop.py 顶部导入：
```python
from step45.runtime_context import (
    RUNTIME_CONTEXT_HISTORY_META,
    RUNTIME_CONTEXT_MESSAGE_META,
)
```

#### 改动 2：`_sanitize_persisted_blocks` 添加 image_url 处理

```python
def _sanitize_persisted_blocks(self, content, *, should_truncate_text=False):
    for block in content:
        if not isinstance(block, dict):
            filtered.append(block)
            continue
        # step45：image_url data: 块替换为占位文本（避免 base64 写入历史）
        if (
            block.get("type") == "image_url"
            and isinstance(block.get("image_url"), dict)
            and str(block["image_url"].get("url", "")).startswith("data:image/")
        ):
            path = (block.get("_meta") or {}).get("path", "")
            filtered.append({
                "type": "text",
                "text": f"[image: {path}]" if path else "[image]",
            })
            continue
        # ... 现有 text truncate 逻辑 ...
```

> 注：最小增量用简单的 `f"[image: {path}]"` 替代 `image_placeholder_text` 函数。完整的 `image_placeholder_text` 留到 media 处理 step（step56）。

#### 改动 3：`updated_at` 类型统一

将两处 `session.updated_at = datetime.now().isoformat()` 改为：
```python
session.updated_at = datetime.now()
```

**风险评估**：
- 需要确认 Session 序列化层是否能处理 datetime 对象
- 如果 Session 用 JSON 序列化，datetime 对象需要自定义 encoder
- 最小增量建议：先改，跑测试确认无回归。如果序列化有问题，回退或添加序列化处理。

### 第二部分：`_state_command` shortcut 持久化（1 个改动点）

#### 改动 4：shortcut 命令持久化 user+assistant

```python
async def _state_command(self, ctx: TurnContext) -> str:
    raw = ctx.msg.content.strip()
    if not raw.startswith("/"):
        return "dispatch"
    cmd_ctx = CommandContext(
        msg=ctx.msg, session=ctx.session,
        key=ctx.session_key, raw=raw, loop=self,
    )
    result = await self.commands.dispatch(cmd_ctx)
    if result is None:
        return "dispatch"
    result.channel = ctx.msg.channel
    result.chat_id = ctx.msg.chat_id
    ctx.outbound = result

    # step45：shortcut 命令持久化 user+assistant（_command 标记）
    # /new 排除（它会清空会话）
    if raw.lower() != "/new":
        ctx.user_persisted_early = self._persist_user_message_early(
            ctx.msg, ctx.session, _command=True,
        )
        ctx.session.add_message(
            "assistant", result.content, _command=True,
        )
        self.sessions.save(ctx.session)
        self._clear_pending_user_turn(ctx.session)

    return "shortcut"
```

**关键点**：
- `_command=True` 标记命令消息
- `_persist_user_message_early` 已支持 `**kwargs`，直接传 `_command=True`
- `session.add_message` 支持 `**kwargs`
- `/new` 命令排除（清空会话，不持久化）

**不做（最小增量）**：
- `is_user_turn` 完整判定（需要 `automation_history_overrides`，留到后续）
- CommandContext 新增 `is_user_turn` 参数（留到后续）

---

## 五、为什么是最小增量

| 做 | 不做（留到后续） |
|----|-----------------|
| `_meta` 弹出 | `is_user_turn` 完整判定 |
| RUNTIME_CONTEXT_MESSAGE_META 处理 | CommandContext.is_user_turn 参数 |
| image_url data: 替换（简单占位） | `image_placeholder_text` 完整函数 |
| updated_at 类型统一 | `_command` 标记的 get_history 过滤 |
| shortcut 持久化 user+assistant | automation_history_overrides 函数 |

**总改动量**：~50 行代码（_save_turn ~20 行 + _sanitize_persisted_blocks ~10 行 + updated_at 2 行 + _state_command ~15 行）。

---

## 六、测试策略（预计 +8 tests）

| 测试 | 验证点 |
|------|--------|
| `test_save_turn_pops_meta` | _save_turn 后消息中无 `_meta` 字段 |
| `test_save_turn_runtime_context_meta` | user 消息中设置 RUNTIME_CONTEXT_HISTORY_META |
| `test_sanitize_image_url_data` | image_url data: 块替换为文本占位 |
| `test_sanitize_image_url_https_unchanged` | https:// 图片 URL 不替换 |
| `test_save_turn_updated_at_is_datetime` | updated_at 是 datetime 对象而非字符串 |
| `test_state_command_persists_shortcut` | shortcut 命令后 session 有 user+assistant 消息 |
| `test_state_command_new_not_persisted` | /new 命令不持久化 |
| `test_state_command_command_marker` | 持久化的消息带 `_command=True` 标记 |

---

## 七、风险与注意事项

### 7.1 updated_at 类型变更风险

`datetime.now()` 对象 vs `datetime.now().isoformat()` 字符串。如果 Session 用 JSON 序列化存储，datetime 对象会失败。需要：
- 检查 Session.save 的序列化方式
- 如果是 JSON，需要添加 datetime 序列化处理或回退

**缓解**：先改，跑测试确认。如果序列化测试失败，添加 `datetime.isoformat()` 在序列化层处理。

### 7.2 `_meta` 弹出可能影响现有逻辑

如果某些代码依赖消息中的 `_meta` 字段（在 _save_turn 之后），弹出会导致问题。但 `_meta` 是内部元数据，不应该持久化，所以弹出是正确的。

### 7.3 shortcut 持久化可能影响现有测试

现有测试可能假设 shortcut 命令不持久化。需要检查并更新相关测试。

### 7.4 `_command` 标记的过滤

nanobot 的 `get_history` 会过滤掉 `_command` 标记的消息（不进入 LLM 上下文）。step45 只添加标记，不过滤。过滤逻辑留到后续。这意味着命令消息会进入 LLM 上下文，可能有轻微影响，但风险低（命令消息通常很短）。

---

## 八、对齐度预期

| 维度 | step44 | step45 后 |
|------|--------|----------|
| _save_turn _meta 弹出 | ❌ | ✅ |
| RUNTIME_CONTEXT_MESSAGE_META 处理 | ❌ | ✅ |
| image_url data: 替换 | ❌ | ✅ |
| updated_at 类型统一 | ❌（字符串） | ✅（对象） |
| shortcut 持久化 | ❌ | ✅ |
| is_user_turn 判定 | ❌ | ❌（后续） |
| _command get_history 过滤 | ❌ | ❌（后续） |

agent 综合对齐度：~83% → ~85%（A4/A39 部分完成）。

---

## 九、下一 step 衔接

- **step46**：runner malformed_retry——不依赖 step45；
- **后续**：`is_user_turn` 完整判定、`_command` 标记的 get_history 过滤、`image_placeholder_text` 完整函数。

step45 是 loop 阶段一的最后一个 step，完成后进入 runner 健壮性对齐阶段（step46-50）。
