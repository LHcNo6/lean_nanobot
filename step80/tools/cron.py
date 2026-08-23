"""定时任务工具：CronTool（step78）。

对齐 nanobot `agent/tools/cron.py` 的最小子集：
- add：创建定时任务（every_seconds / cron_expr / at）；
- list：列出所有定时任务；
- remove：删除指定任务。

简化版：用内存存储管理任务元数据，不实现真正的后台执行。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from step80.schema import IntegerSchema, StringSchema, tool_parameters_schema
from step80.tool import Tool, ToolResult, tool_parameters


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class _CronJob:
    """定时任务元数据。"""

    job_id: str
    name: str
    message: str
    every_seconds: int = 0
    cron_expr: str = ""
    at: str = ""
    tz: str = "UTC"
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())


class _CronStore:
    """内存定时任务存储。"""

    def __init__(self) -> None:
        self._jobs: dict[str, _CronJob] = {}

    def add(self, job: _CronJob) -> None:
        """添加任务。"""
        self._jobs[job.job_id] = job

    def list(self) -> list[_CronJob]:
        """列出所有任务。"""
        return list(self._jobs.values())

    def remove(self, job_id: str) -> bool:
        """删除任务，返回是否存在。"""
        if job_id in self._jobs:
            del self._jobs[job_id]
            return True
        return False

    def get(self, job_id: str) -> _CronJob | None:
        """获取任务。"""
        return self._jobs.get(job_id)


# ---------------------------------------------------------------------------
# CronTool
# ---------------------------------------------------------------------------


@tool_parameters(tool_parameters_schema(
    action=StringSchema("Action to perform: add, list, or remove", enum=["add", "list", "remove"]),
    name=StringSchema("Optional short label for the job (e.g. 'daily-standup')"),
    message=StringSchema("REQUIRED for action='add'. Instruction to execute when the job triggers."),
    every_seconds=IntegerSchema("Interval in seconds for recurring tasks (0 = disabled)", minimum=0, maximum=86400 * 365),
    cron_expr=StringSchema("Cron expression like '0 9 * * *' for scheduled tasks"),
    at=StringSchema("ISO datetime for one-time execution (e.g. '2026-12-01T10:30:00')"),
    tz=StringSchema("IANA timezone (e.g. 'Asia/Shanghai'). Default: UTC"),
    job_id=StringSchema("REQUIRED for action='remove'. Job ID to remove."),
    required=["action"],
))
class CronTool(Tool):
    """定时任务工具：创建、列出、删除定时任务。

    简化版：任务元数据存储在内存中，不实现真正的后台执行。
    """

    _scopes = {"core"}

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        """是否启用：需要 ctx.cron_store。"""
        return getattr(ctx, "cron_store", None) is not None

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        """从上下文创建工具实例。"""
        store = getattr(ctx, "cron_store", None)
        if store is None:
            store = _CronStore()
        return cls(cron_store=store)

    def __init__(self, cron_store: _CronStore):
        """初始化 CronTool。

        Args:
            cron_store: 定时任务存储。
        """
        self._store = cron_store

    @property
    def name(self) -> str:
        """工具名：``cron``。"""
        return "cron"

    @property
    def description(self) -> str:
        """工具描述。"""
        return (
            "Schedule reminders and recurring tasks. "
            "Use action='add' to create a job (with message + one schedule: "
            "every_seconds, cron_expr, or at), action='list' to view jobs, "
            "action='remove' to delete a job by job_id."
        )

    @property
    def read_only(self) -> bool:
        """cron 工具不是只读（可以创建/删除任务）。"""
        return False

    async def execute(
        self,
        action: str | None = None,
        name: str | None = None,
        message: str | None = None,
        every_seconds: int = 0,
        cron_expr: str | None = None,
        at: str | None = None,
        tz: str = "UTC",
        job_id: str | None = None,
        **kwargs: Any,
    ) -> str | ToolResult:
        """执行定时任务操作。

        Args:
            action: "add", "list", 或 "remove"。
            name: 任务名称。
            message: 触发时执行的指令（add 必填）。
            every_seconds: 间隔秒数。
            cron_expr: cron 表达式。
            at: ISO datetime（一次性）。
            tz: 时区。
            job_id: 要删除的任务 ID（remove 必填）。
            **kwargs: 忽略的额外参数。

        Returns:
            成功时返回文本；失败时返回 ``ToolResult.error``。
        """
        if not action:
            return ToolResult.error("Error: 'action' is required (add, list, or remove).")

        action = action.lower().strip()

        if action == "add":
            return self._do_add(name, message, every_seconds, cron_expr, at, tz)
        elif action == "list":
            return self._do_list()
        elif action == "remove":
            return self._do_remove(job_id)
        else:
            return ToolResult.error(f"Error: unknown action '{action}'. Use add, list, or remove.")

    def _do_add(
        self,
        name: str | None,
        message: str | None,
        every_seconds: int,
        cron_expr: str | None,
        at: str | None,
        tz: str,
    ) -> str | ToolResult:
        """执行 add 操作。"""
        if not message:
            return ToolResult.error("Error: 'message' is required for action='add'.")

        # 至少需要一个调度参数
        has_schedule = every_seconds > 0 or bool(cron_expr) or bool(at)
        if not has_schedule:
            return ToolResult.error(
                "Error: add requires one schedule: every_seconds (>0), "
                "cron_expr, or at."
            )

        job_id = uuid.uuid4().hex[:12]
        job_name = name or message[:30]

        job = _CronJob(
            job_id=job_id,
            name=job_name,
            message=message,
            every_seconds=every_seconds,
            cron_expr=cron_expr or "",
            at=at or "",
            tz=tz,
        )
        self._store.add(job)

        schedule_desc = []
        if every_seconds > 0:
            schedule_desc.append(f"every {every_seconds}s")
        if cron_expr:
            schedule_desc.append(f"cron '{cron_expr}'")
        if at:
            schedule_desc.append(f"at {at}")

        return (
            f"Created cron job {job_id} ({job_name}): "
            f"{', '.join(schedule_desc)} [{tz}]"
        )

    def _do_list(self) -> str:
        """执行 list 操作。"""
        jobs = self._store.list()

        if not jobs:
            return "No cron jobs scheduled."

        lines = [f"Scheduled {len(jobs)} cron job(s):"]
        for job in jobs:
            schedule = []
            if job.every_seconds > 0:
                schedule.append(f"every {job.every_seconds}s")
            if job.cron_expr:
                schedule.append(f"cron '{job.cron_expr}'")
            if job.at:
                schedule.append(f"at {job.at}")
            lines.append(
                f"  [{job.job_id}] {job.name} - {', '.join(schedule)} [{job.tz}]"
            )
            lines.append(f"    message: {job.message}")

        return "\n".join(lines)

    def _do_remove(self, job_id: str | None) -> str | ToolResult:
        """执行 remove 操作。"""
        if not job_id:
            return ToolResult.error("Error: 'job_id' is required for action='remove'.")

        if self._store.remove(job_id):
            return f"Removed cron job {job_id}."
        else:
            return ToolResult.error(f"Error: cron job '{job_id}' not found.")
