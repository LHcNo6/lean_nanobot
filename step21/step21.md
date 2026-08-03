# Step 21 — CommandRouter & COMMAND 状态

在 Step 20 (Channel Framework) 基础上，对齐 nanobot 的 agent 核心状态机：`loop.py`
补上缺失的 `COMMAND` 状态（8 态对齐），并引入 `CommandRouter`（priority/exact/prefix 三档）
与内置命令。`main.py` 中手写的 `on_command` 闭包被删除，命令从此真正"走进 loop"。

---

## 一、这一阶段解决了什么问题、为什么要这样做

Step 20 完成时，loop 是 7 态（`RESTORE→COMPACT→BUILD→RUN→SAVE→RESPOND→DONE`），
而 nanobot 是 8 态，中间多了 `COMMAND`。slash 命令在那时由 `CliChannel.on_command`
回调在通道层拦截（`/dream` `/history` `/new` 手写在 main.py 的 if/elif 里、
`/pairing` 有 `PairingStore.handle_pairing_command` 但根本没接线）。

问题有三：

- **结构不对齐**：命令处理不属于 agent 核心状态机，无法复用 session 锁、无法短路，
  与 nanobot 的「COMMAND 态 + CommandRouter + Builtin 命令」模型相差一个维度；
- **散落与重复**：每条命令的手写逻辑不能注册 / 覆盖 / 复用，硬编码在程序组装处；
- **权限与命名不对齐**：`normalize_command_text`（bot 后缀剥离）、三档路由优先级
  这些传输层语义在 nanobot 都是统一的。

因此 step21 把命令处理**上升为 loop 第一公民状态**，与 nanobot `agent/loop.py`
 的 `CommandContext` / `dispatch` / `shortcut` 语义一致。

## 二、目标与实现

| 目标 | 实现 |
|------|------|
| 命令进状态机 | `loop.py` 增 `COMMAND` 态；`(COMPACT,"ok")→COMMAND`、`(COMMAND,"dispatch")→BUILD`、`(COMMAND,"shortcut")→DONE` |
| 三档路由 | `command/router.py`：`CommandRouter.priority/exact/prefix` + `normalize_command_text` + `CommandContext` |
| 内置命令 | `command/builtin.py`：`/help` `/dream` `/history` `/new` `/pairing`（接 PairingStore） |
| main.py 瘦身 | 删除 `on_command` 闭包；`/exit` 仍由 CliChannel 原生处理；`PairingStore` 注入 loop 与 ChannelManager |
| 命令不污染上下文 | 命令对不写入 session（决策见"暴露的问题"） |

## 三、核心函数 / 类说明

### `command/router.py`
- `normalize_command_text(text)`：剥离 Telegram/Discord 式 `@bot` 后缀，保留用户参数（对齐 nanobot `command/router.py`）。
- `CommandRouter`：`priority`（session 锁外，预留）/`exact`（精确）/`prefix`（最长前缀优先）三档；`dispatch()` 匹配 exact → prefix 并填充 `ctx.args`；未命中返回 `None`。
- `CommandContext`：`msg`/`session`/`key`/`raw`/`args`/`loop`。handler 通过 `ctx.loop` 解耦依赖（session、pairing、run_dream）。

### `command/builtin.py`
- `/help`：命令清单。
- `/dream`：`ctx.loop.run_dream()` → 有结果返回内容，否则 `[Dream] Nothing to process.`。
- `/history`：打印 `session.messages`（含 `last_consolidated` 标记与 goal 摘要）。
- `/new`：`sessions.invalidate(key)` + 删除会话文件。
- `/pairing [list|approve|deny|revoke]`：`ctx.loop.pairing.handle_pairing_command(channel, args)`。

### `loop.py`
- `_state_command(ctx)`：内容非 `/` 开头 → `"dispatch"` → BUILD（正常 agent 回合）；
  命中命令 → 回填 `outbound.channel/chat_id`，返回 `"shortcut"` → DONE；
  未命中的 `/...` 也落入 BUILD（agent 按普通文本处理，对齐 nanobot）。
- `AgentLoop.__init__(..., pairing=None)`：新增可选字段，向后兼容。

### `main.py`
- `PairingStore` 创建提前；同时注入 `AgentLoop` 与 `ChannelManager`。
- 启动提示改为 `/help`；`/exit` 保持 CliChannel 原生。

## 三、暴露的问题 / 偏离与取舍

| 取舍 | 说明 | 后续计划 |
|------|------|----------|
| 命令对不持久化 | 不对齐 nanbot 的 `_command=True` 落 session（其动机是 WebUI 历史），我们无 WebUI；不写避免污染 token 预算/合并逻辑 | 若后续引入 WebUI，再补持久化与 `get_history` 过滤 `_command` |
| priority 档空转 | API 与 nanbot 一致但无消费方（无 `/stop` 类命令） | 网关/重启场景再启用 |
| 未知 `/xxx` 进 agent | 对齐 nanbot 的 `dispatch=None → BUILD` | 无 |
| session 锁 | 命令在 `_dispatch` 的 session 锁内执行（内置命令都是 shortcut 无等待） | 若需 `/stop` 打断长任务，参照 nanbot 加 priority 档在锁外执行 |

## 四、下一步要解决什么

Step 22 — Providers Registry & Factory + Fallback（异常式）：`providers/registry.py`
（ProviderSpec + find_by_name）、`providers/factory.py`、`providers/fallback_provider.py`。
替换 `from_env()` 单例；main.py 改为工厂装配。仍是纯 dataclass、不依赖 pydantic。