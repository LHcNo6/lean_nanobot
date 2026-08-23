"""交互式执行会话：长运行命令管理（step73）。

对齐 nanobot `agent/tools/exec_session.py` 的最小子集：
- ``_ExecSession``：管理单个长运行进程（后台读流 + 输出缓冲 + stdin）；
- ``ExecSessionManager``：管理多个会话（start/write/poll）；
- ``WriteStdinTool``：向会话写入 stdin 的工具。

简化了 nanobot 的高级特性（owner_session_key 隔离、idle_timeout 自动清理、
shell_program/login、会话列表查询、复杂输出格式化）。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import Any

from step90.schema import BooleanSchema, IntegerSchema, StringSchema, tool_parameters_schema
from step90.tool import Tool, ToolResult, tool_parameters

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

DEFAULT_YIELD_MS = 1000
MAX_YIELD_MS = 30_000
DEFAULT_MAX_OUTPUT_CHARS = 10_000
MAX_OUTPUT_CHARS = 50_000


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SessionPoll:
    """会话轮询结果。"""

    output: str
    done: bool
    exit_code: int | None
    elapsed_s: float = 0.0
    timed_out: bool = False


# ---------------------------------------------------------------------------
# 输出截断
# ---------------------------------------------------------------------------


def _truncate_output(text: str, max_len: int) -> tuple[str, int]:
    """截断输出，返回 (截断后文本, 截断字符数)。"""
    if len(text) <= max_len:
        return text, 0
    half = max_len // 2
    truncated = len(text) - max_len
    return (
        text[:half] + f"\n\n... ({truncated:,} chars truncated) ...\n\n" + text[-half:],
        truncated,
    )


def _clamp(value: int | None, default: int, minimum: int, maximum: int) -> int:
    """夹紧整数到 [minimum, maximum] 范围。"""
    if value is None:
        return default
    return max(minimum, min(value, maximum))


# ---------------------------------------------------------------------------
# _ExecSession
# ---------------------------------------------------------------------------


class _ExecSession:
    """单个执行会话：管理长运行进程及其输出。

    后台任务持续读取 stdout/stderr，输出缓冲在 ``_chunks`` 中。
    ``poll`` 收集并清空缓冲，返回新输出。
    """

    def __init__(
        self,
        *,
        session_id: str,
        process: asyncio.subprocess.Process,
        command: str,
        cwd: str,
        timeout: int | None,
    ):
        """初始化会话。

        Args:
            session_id: 会话 ID。
            process: asyncio 子进程。
            command: 执行的命令。
            cwd: 工作目录。
            timeout: 超时秒数（None=不限制）。
        """
        self.session_id = session_id
        self.process = process
        self.command = command
        self.cwd = cwd
        self.started_at = time.monotonic()
        # timeout None/0 表示不限制
        self.deadline = time.monotonic() + timeout if timeout else float("inf")
        self.last_access = time.monotonic()
        self._chunks: list[str] = []
        self._lock = asyncio.Lock()
        self._timed_out = False
        # 启动后台读取任务
        self._stdout_task = asyncio.create_task(self._read_stream(process.stdout, ""))
        self._stderr_task = asyncio.create_task(self._read_stream(process.stderr, "STDERR:\n"))

    async def _read_stream(self, stream: asyncio.StreamReader | None, prefix: str) -> None:
        """后台持续读取流，追加到输出缓冲。

        Args:
            stream: 流读取器。
            prefix: 首次输出前缀（如 "STDERR:\n"）。
        """
        if stream is None:
            return
        first = True
        while True:
            try:
                chunk = await stream.read(4096)
            except (asyncio.CancelledError, Exception):
                break
            if not chunk:
                break
            text = chunk.decode("utf-8", errors="replace")
            if prefix and first:
                text = prefix + text
                first = False
            async with self._lock:
                self._chunks.append(text)

    async def poll(
        self,
        yield_time_ms: int,
        max_output_chars: int,
    ) -> SessionPoll:
        """轮询会话输出。

        等待 yield_time_ms 或进程退出，然后收集输出。

        Args:
            yield_time_ms: 等待毫秒数。
            max_output_chars: 最大输出字符数。

        Returns:
            SessionPoll 结果。
        """
        self.last_access = time.monotonic()

        # 等待进程退出或超时
        if yield_time_ms > 0 and self.process.returncode is None:
            wait_s = min(yield_time_ms, MAX_YIELD_MS) / 1000
            remaining_s = self.deadline - time.monotonic()
            if remaining_s <= 0:
                wait_s = 0
            else:
                wait_s = min(wait_s, remaining_s)
            if wait_s > 0:
                try:
                    await asyncio.wait_for(self.process.wait(), timeout=wait_s)
                except asyncio.TimeoutError:
                    pass

        # 超时检查
        if self.process.returncode is None and time.monotonic() >= self.deadline:
            self._timed_out = True
            await self.kill()

        # 进程退出后等待读取任务完成
        if self.process.returncode is not None:
            try:
                await asyncio.wait_for(
                    asyncio.gather(self._stdout_task, self._stderr_task),
                    timeout=2.0,
                )
            except (asyncio.TimeoutError, Exception):
                pass

        # 收集输出
        async with self._lock:
            output = "".join(self._chunks)
            self._chunks.clear()

        output, _ = _truncate_output(output, max_output_chars)
        return SessionPoll(
            output=output,
            done=self.process.returncode is not None,
            exit_code=self.process.returncode,
            elapsed_s=max(0.0, time.monotonic() - self.started_at),
            timed_out=self._timed_out,
        )

    async def write(self, chars: str) -> str | None:
        """向 stdin 写入字符。

        Args:
            chars: 要写入的字符。

        Returns:
            错误消息（失败时）或 None（成功时）。
        """
        if self.process.returncode is not None:
            return "session has already exited"
        if self.process.stdin is None:
            return "session stdin is not available"
        try:
            self.process.stdin.write(chars.encode("utf-8"))
            await self.process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, AttributeError, OSError) as exc:
            return f"session stdin write failed: {exc}"
        return None

    async def close_stdin(self) -> str | None:
        """关闭 stdin。

        Returns:
            错误消息（失败时）或 None（成功时）。
        """
        if self.process.returncode is not None:
            return "session has already exited"
        if self.process.stdin is None:
            return "session stdin is not available"
        self.process.stdin.close()
        try:
            await self.process.stdin.wait_closed()
        except (BrokenPipeError, ConnectionResetError):
            pass
        return None

    async def kill(self) -> None:
        """杀死进程。"""
        if self.process.returncode is not None:
            return
        self.process.kill()
        try:
            await asyncio.wait_for(self.process.wait(), timeout=5.0)
        except (asyncio.TimeoutError, Exception):
            pass


# ---------------------------------------------------------------------------
# ExecSessionManager
# ---------------------------------------------------------------------------


class ExecSessionManager:
    """执行会话管理器：创建、管理、清理多个会话。"""

    def __init__(self, *, max_sessions: int = 8):
        """初始化管理器。

        Args:
            max_sessions: 最大并发会话数。
        """
        self.max_sessions = max_sessions
        self._sessions: dict[str, _ExecSession] = {}
        self._lock = asyncio.Lock()

    async def start(
        self,
        *,
        command: str,
        cwd: str,
        env: dict[str, str],
        timeout: int | None,
        yield_time_ms: int,
        max_output_chars: int,
    ) -> tuple[str, SessionPoll]:
        """启动新会话。

        Args:
            command: 要执行的命令。
            cwd: 工作目录。
            env: 环境变量。
            timeout: 超时秒数。
            yield_time_ms: 首次轮询等待毫秒。
            max_output_chars: 最大输出字符数。

        Returns:
            (session_id, 首次 poll 结果)。

        Raises:
            RuntimeError: 达到最大会话数。
        """
        async with self._lock:
            if len(self._sessions) >= self.max_sessions:
                raise RuntimeError(f"maximum exec sessions reached ({self.max_sessions})")

            process = await asyncio.create_subprocess_shell(
                command,
                cwd=cwd,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            session_id = uuid.uuid4().hex[:12]
            session = _ExecSession(
                session_id=session_id,
                process=process,
                command=command,
                cwd=cwd,
                timeout=timeout,
            )
            self._sessions[session_id] = session

        # 首次轮询（在锁外执行，避免阻塞其他操作）
        poll = await session.poll(yield_time_ms, max_output_chars)

        # 如果已完成，从管理器移除
        if poll.done:
            async with self._lock:
                self._sessions.pop(session_id, None)

        return session_id, poll

    async def write(
        self,
        *,
        session_id: str,
        chars: str | None,
        close_stdin: bool,
        terminate: bool,
        yield_time_ms: int,
        max_output_chars: int,
    ) -> SessionPoll:
        """向会话写入 stdin 并轮询输出。

        Args:
            session_id: 目标会话 ID。
            chars: 要写入的字符（None=不写入）。
            close_stdin: 是否关闭 stdin。
            terminate: 是否终止进程。
            yield_time_ms: 轮询等待毫秒。
            max_output_chars: 最大输出字符数。

        Returns:
            SessionPoll 结果。

        Raises:
            KeyError: 会话不存在。
            RuntimeError: 写入失败。
        """
        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(session_id)

        if chars:
            error = await session.write(chars)
            if error:
                raise RuntimeError(error)

        if close_stdin:
            error = await session.close_stdin()
            if error:
                raise RuntimeError(error)

        if terminate:
            await session.kill()

        poll = await session.poll(yield_time_ms, max_output_chars)

        if poll.done:
            async with self._lock:
                self._sessions.pop(session_id, None)

        return poll

    def get(self, session_id: str) -> _ExecSession | None:
        """获取会话。

        Args:
            session_id: 会话 ID。

        Returns:
            会话实例，不存在时返回 None。
        """
        return self._sessions.get(session_id)


# ---------------------------------------------------------------------------
# 结果格式化
# ---------------------------------------------------------------------------


def _format_session_poll(session_id: str, poll: SessionPoll, *, started: bool = False) -> str:
    """格式化会话轮询结果为文本。

    Args:
        session_id: 会话 ID。
        poll: 轮询结果。
        started: 是否是首次启动（显示 Session started 提示）。

    Returns:
        格式化后的文本。
    """
    lines: list[str] = []
    if started:
        lines.append(f"Session started: {session_id}")

    if poll.output:
        lines.append(poll.output)

    if poll.done:
        if poll.timed_out:
            lines.append(f"[timed out, exit code: {poll.exit_code}]")
        else:
            lines.append(f"[exit code: {poll.exit_code}]")
    else:
        lines.append("[still running]")

    lines.append(f"Elapsed: {poll.elapsed_s:.1f}s")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# WriteStdinTool
# ---------------------------------------------------------------------------


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("The exec session ID to write to"),
        chars=StringSchema("Characters to write to stdin"),
        close_stdin=BooleanSchema(description="Close stdin after writing (default false)", default=False),
        terminate=BooleanSchema(description="Terminate the session (default false)", default=False),
        yield_time_ms=IntegerSchema(
            "Milliseconds to wait for output after writing (default 1000)",
            minimum=0,
            maximum=MAX_YIELD_MS,
        ),
        max_output_chars=IntegerSchema(
            "Maximum output characters to return (default 10000)",
            minimum=1000,
            maximum=MAX_OUTPUT_CHARS,
        ),
        required=["session_id"],
    )
)
class WriteStdinTool(Tool):
    """向执行会话写入 stdin 的工具。

    功能：
    - 向指定会话写入字符；
    - 关闭 stdin；
    - 终止会话；
    - 轮询输出。

    对齐 nanobot ``exec_session.WriteStdinTool``，简化了 owner_session_key 隔离。
    """

    _scopes = {"core", "subagent"}

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        """是否启用：需要 exec_session_manager。"""
        return getattr(ctx, "exec_session_manager", None) is not None

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        """从上下文创建工具实例，注入 session_manager。"""
        tool = cls()
        tool._session_manager = getattr(ctx, "exec_session_manager", None)
        return tool

    @property
    def name(self) -> str:
        """工具名：``write_stdin``。"""
        return "write_stdin"

    @property
    def description(self) -> str:
        """工具描述。"""
        return (
            "Write to stdin of a running exec session, optionally close stdin "
            "or terminate, then return new output. Use the session_id returned "
            "by exec with yield_time_ms."
        )

    @property
    def read_only(self) -> bool:
        """write_stdin 不是只读操作。"""
        return False

    async def execute(
        self,
        session_id: str | None = None,
        chars: str | None = None,
        close_stdin: bool = False,
        terminate: bool = False,
        yield_time_ms: int | None = None,
        max_output_chars: int | None = None,
        **kwargs: Any,
    ) -> str | ToolResult:
        """执行 stdin 写入。

        Args:
            session_id: 目标会话 ID（必填）。
            chars: 要写入的字符。
            close_stdin: 是否关闭 stdin。
            terminate: 是否终止进程。
            yield_time_ms: 轮询等待毫秒。
            max_output_chars: 最大输出字符数。
            **kwargs: 忽略的额外参数。

        Returns:
            成功时返回格式化的输出文本；失败时返回 ``ToolResult.error``。
        """
        if not session_id:
            return ToolResult.error("Error: Missing session_id parameter.")

        manager = getattr(self, "_session_manager", None)
        if manager is None:
            return ToolResult.error("Error: No exec session manager available.")

        effective_yield = _clamp(yield_time_ms, DEFAULT_YIELD_MS, 0, MAX_YIELD_MS)
        effective_max = _clamp(max_output_chars, DEFAULT_MAX_OUTPUT_CHARS, 1000, MAX_OUTPUT_CHARS)

        try:
            poll = await manager.write(
                session_id=session_id,
                chars=chars,
                close_stdin=close_stdin,
                terminate=terminate,
                yield_time_ms=effective_yield,
                max_output_chars=effective_max,
            )
        except KeyError:
            return ToolResult.error(f"Error: Session not found: {session_id}")
        except RuntimeError as exc:
            return ToolResult.error(f"Error: {exc}")
        except Exception as exc:
            return ToolResult.error(f"Error: write_stdin failed: {exc}")

        return _format_session_poll(session_id, poll)
