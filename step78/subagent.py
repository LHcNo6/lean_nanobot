from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from step78.bus import MessageBus
from step78.bus.events import InboundMessage
from step78.hook import AgentHook, AgentHookContext
from step78.provider import LLMProvider
from step78.runner import AgentRunSpec, AgentRunner
from step78.tool import ToolRegistry


@dataclass
class SubagentStatus:
    task_id: str
    label: str
    task_description: str
    started_at: float = 0.0
    phase: str = "initializing"
    iteration: int = 0
    tool_events: list[Any] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    stop_reason: str | None = None
    error: str | None = None


class _SubagentHook(AgentHook):
    def __init__(self, task_id: str, status: SubagentStatus | None = None) -> None:
        super().__init__()
        self._task_id = task_id
        self._status = status

    async def after_iteration(self, context: AgentHookContext) -> None:
        if self._status is None:
            return
        self._status.iteration = context.iteration
        self._status.tool_events = list(context.tool_calls)
        self._status.usage = dict(context.usage)
        if context.error:
            self._status.error = str(context.error)


_SUBAGENT_SYSTEM_PROMPT = """You are a subagent spawned by the main agent.
Stay focused on the assigned task. Use tools to complete it.
Your final response will be reported back to the main agent."""


class SubagentManager:
    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider | None = None,
        tools: ToolRegistry | None = None,
        max_concurrent_subagents: int = 5,
        max_iterations: int = 10,
    ):
        self.bus = bus
        self._provider = provider
        self._tools = tools or ToolRegistry()
        self.max_concurrent_subagents = max_concurrent_subagents
        self.max_iterations = max_iterations
        self.runner = AgentRunner()
        self._running_tasks: dict[str, asyncio.Task] = {}
        self._task_statuses: dict[str, SubagentStatus] = {}
        self._session_tasks: dict[str, set[str]] = {}

    async def spawn(
        self,
        task: str,
        label: str | None = None,
        origin_channel: str = "cli",
        origin_chat_id: str = "direct",
        session_key: str | None = None,
    ) -> str:
        running = self.get_running_count()
        if running >= self.max_concurrent_subagents:
            return (
                f"Cannot spawn subagent: concurrency limit reached "
                f"({running}/{self.max_concurrent_subagents} running)."
            )
        task_id = str(uuid.uuid4())[:8]
        display_label = label or task[:30] + ("..." if len(task) > 30 else "")

        status = SubagentStatus(
            task_id=task_id,
            label=display_label,
            task_description=task,
            started_at=0.0,
        )
        self._task_statuses[task_id] = status

        origin = {"channel": origin_channel, "chat_id": origin_chat_id, "session_key": session_key}

        bg_task = asyncio.create_task(self._run_subagent(task_id, task, display_label, origin, status))
        self._running_tasks[task_id] = bg_task
        if session_key:
            self._session_tasks.setdefault(session_key, set()).add(task_id)

        def _cleanup(_):
            self._running_tasks.pop(task_id, None)
            self._task_statuses.pop(task_id, None)
            if session_key and (ids := self._session_tasks.get(session_key)):
                ids.discard(task_id)
                if not ids:
                    del self._session_tasks[session_key]

        bg_task.add_done_callback(_cleanup)
        return f"Subagent [{display_label}] started (id: {task_id}). I'll report back when done."

    async def _run_subagent(self, task_id: str, task: str, label: str, origin: dict, status: SubagentStatus) -> None:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _SUBAGENT_SYSTEM_PROMPT},
            {"role": "user", "content": task},
        ]
        if self._provider is None:
            return
        try:
            result = await self.runner.run(AgentRunSpec(
                initial_messages=messages,
                tools=self._tools,
                provider=self._provider,
                max_iterations=self.max_iterations,
                hook=_SubagentHook(task_id, status),
            ))
            status.phase = "done"
            status.stop_reason = result.stop_reason
            final = result.final_content or "Task completed."
            await self._announce(task_id, label, task, final, origin, "ok")
        except Exception as e:
            status.phase = "error"
            status.error = str(e)
            await self._announce(task_id, label, task, f"Error: {e}", origin, "error")

    async def _announce(self, task_id: str, label: str, task: str, result: str, origin: dict, status: str) -> None:
        status_text = "completed" if status == "ok" else "failed"
        content = (
            f"[Subagent '{label}' {status_text}]\n\n"
            f"Task: {task}\n\nResult:\n{result}\n\n"
            f"Summarize this naturally for the user. Keep it brief."
        )
        override = origin.get("session_key") or f"{origin['channel']}:{origin['chat_id']}"
        metadata = {"injected_event": "subagent_result", "subagent_task_id": task_id}
        msg = InboundMessage(
            channel="system",
            sender_id="subagent",
            chat_id=override,
            content=content,
            session_key_override=override,
            metadata=metadata,
        )
        await self.bus.publish_inbound(msg)

    async def cancel_by_session(self, session_key: str) -> int:
        tasks = [self._running_tasks[tid] for tid in self._session_tasks.get(session_key, [])
                 if tid in self._running_tasks and not self._running_tasks[tid].done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return len(tasks)

    def get_running_count(self) -> int:
        return len(self._running_tasks)

    def get_running_count_by_session(self, session_key: str) -> int:
        tids = self._session_tasks.get(session_key, set())
        return sum(1 for tid in tids if tid in self._running_tasks and not self._running_tasks[tid].done())
