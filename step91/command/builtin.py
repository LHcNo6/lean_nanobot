"""Built-in slash commands.

对齐 nanobot `command/builtin.py` 的最小集。命令通过 `CommandContext.loop`
解耦依赖（AgentLoop 持有 sessions / pairing / run_dream 等）。命令命中后由
loop 的 COMMAND 状态短路（shortcut → DONE），本次不持久化命令对到会话，
避免污染 LLM 历史 / token 预算；`/history` 直接打印会话保证可见性。
"""

from __future__ import annotations

from step91.command.router import CommandContext, CommandRouter
from step91.bus.events import OutboundMessage
from step91.goal_state import goal_state_runtime_lines


async def _cmd_help(ctx: CommandContext) -> OutboundMessage:
    return OutboundMessage(content=(
        "Commands:\n"
        "  /help — show this help\n"
        "  /stop — cancel the active turn (restores partial progress)\n"
        "  /history — show session history\n"
        "  /new — reset the current session\n"
        "  /dream — trigger memory consolidation now\n"
        "  /pairing [list|approve <code>|deny <code>|revoke <user>|revoke <channel> <user>]\n"
        "  /exit — quit (handled by the CLI channel)"
    ))


async def _cmd_stop(ctx: CommandContext) -> OutboundMessage:
    """取消当前会话的活动任务（A13，priority 档在线无锁执行）。

    取消后 `_dispatch` 的 CancelledError 分支会把 checkpoint 物化回会话，
    用户不会丢失 /stop 前已完成的工具结果。
    """
    loop = ctx.loop
    if loop is None:
        return OutboundMessage(content="[Stop] Agent loop unavailable.")
    total = await loop._cancel_active_tasks(ctx.key)
    content = f"Stopped {total} task(s)." if total else "No active task to stop."
    return OutboundMessage(content=content)


async def _cmd_dream(ctx: CommandContext) -> OutboundMessage:
    result = await ctx.loop.run_dream()
    if result and result.final_content:
        return OutboundMessage(content=f"[Dream result]\n{result.final_content[:300]}")
    return OutboundMessage(content="[Dream] Nothing to process.")


async def _cmd_history(ctx: CommandContext) -> OutboundMessage:
    session = ctx.session
    lines: list[str] = []
    if session is None:
        return OutboundMessage(content="No session.\n")
    # step29：隐藏历史行（subagent 注入结果等，HIDDEN_HISTORY_META）只服务
    # 模型上下文，不作为聊天 turn 展示。
    from step91.session.history_visibility import is_hidden_history_message
    lines.append("--- Session History ---")
    for i, m in enumerate(session.messages):
        if is_hidden_history_message(m):
            continue
        role = m["role"].ljust(9)
        content = (m.get("content") or "")[:80]
        name = m.get("name", "")
        extra = f" ({name})" if name else ""
        lc = " <-- last_consolidated" if i == session.last_consolidated else ""
        lines.append(f"  [{i}] {role}{content}{extra}{lc}")
    goal_lines = goal_state_runtime_lines(session.metadata)
    if goal_lines:
        lines.append("  [goal] " + " | ".join(goal_lines[:2]))
    lines.append("---")
    return OutboundMessage(content="\n".join(lines))


async def _cmd_new(ctx: CommandContext) -> OutboundMessage:
    ctx.loop.sessions.invalidate(ctx.key)
    path = ctx.loop.sessions._get_session_path(ctx.key)
    if path.exists():
        path.unlink()
    return OutboundMessage(content="Session reset.")


async def _cmd_pairing(ctx: CommandContext) -> OutboundMessage:
    store = getattr(ctx.loop, "pairing", None)
    if store is None:
        return OutboundMessage(content="Pairing is not enabled.")
    text = store.handle_pairing_command(ctx.msg.channel, ctx.args.strip())
    return OutboundMessage(content=text)


def register_builtin_commands(router: CommandRouter) -> None:
    router.exact("/help", _cmd_help)
    router.exact("/dream", _cmd_dream)
    router.exact("/history", _cmd_history)
    router.exact("/new", _cmd_new)
    router.exact("/pairing", _cmd_pairing)
    router.prefix("/pairing ", _cmd_pairing)
    # step29：/stop 走 priority 档——run() 消费循环内联执行，不占会话锁，
    # 因此能打断会话内正在进行的 turn（其余命令都需要会话锁）。
    router.priority("/stop", _cmd_stop)