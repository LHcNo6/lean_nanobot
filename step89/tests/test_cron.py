"""step78：CronTool 定时任务单元测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from step89.context import ToolContext
from step89.loader import ToolLoader
from step89.tool import ToolRegistry, ToolResult
from step89.tools.cron import CronTool, _CronStore


def _make_config() -> SimpleNamespace:
    return SimpleNamespace(
        exec=SimpleNamespace(enable=True, timeout=60, sandbox="", allowed_env_keys=[], allow_patterns=[], deny_patterns=[], path_prepend="", path_append=""),
        tools=SimpleNamespace(restrict_to_workspace=False, file=SimpleNamespace(enable=True)),
        web=SimpleNamespace(enable=True, timeout=30, user_agent="Test", search=SimpleNamespace(provider="duckduckgo", max_results=5, timeout=30)),
        my=SimpleNamespace(enable=True, allow_set=False),
    )


def _make_ctx(workspace: str) -> ToolContext:
    from step89.tools.file_state import FileStateStore
    return ToolContext(
        config=_make_config(),
        workspace=workspace,
        restrict_to_workspace=False,
        session_key="test-session",
        file_state_store=FileStateStore(),
        cron_store=_CronStore(),
    )


def _run(coro):
    return asyncio.run(coro)


class TestCronAdd:
    """add 操作。"""

    def test_add_every_seconds(self, tmp_path: Path) -> None:
        """用 every_seconds 创建任务。"""
        ctx = _make_ctx(str(tmp_path))
        tool = CronTool.create(ctx)

        result = _run(tool.execute(action="add", message="Check status", every_seconds=60))
        assert "Created cron job" in str(result)
        assert "every 60s" in str(result)

    def test_add_cron_expr(self, tmp_path: Path) -> None:
        """用 cron_expr 创建任务。"""
        ctx = _make_ctx(str(tmp_path))
        tool = CronTool.create(ctx)

        result = _run(tool.execute(action="add", message="Daily standup", cron_expr="0 9 * * *"))
        assert "Created cron job" in str(result)
        assert "cron '0 9 * * *'" in str(result)

    def test_add_at(self, tmp_path: Path) -> None:
        """用 at 创建一次性任务。"""
        ctx = _make_ctx(str(tmp_path))
        tool = CronTool.create(ctx)

        result = _run(tool.execute(action="add", message="Reminder", at="2026-12-01T10:30:00"))
        assert "Created cron job" in str(result)
        assert "at 2026-12-01T10:30:00" in str(result)

    def test_add_missing_message(self, tmp_path: Path) -> None:
        """add 缺少 message 报错。"""
        ctx = _make_ctx(str(tmp_path))
        tool = CronTool.create(ctx)

        result = _run(tool.execute(action="add", every_seconds=60))
        assert isinstance(result, ToolResult)
        assert result.is_error

    def test_add_missing_schedule(self, tmp_path: Path) -> None:
        """add 缺少调度参数报错。"""
        ctx = _make_ctx(str(tmp_path))
        tool = CronTool.create(ctx)

        result = _run(tool.execute(action="add", message="Test"))
        assert isinstance(result, ToolResult)
        assert result.is_error

    def test_add_with_name(self, tmp_path: Path) -> None:
        """add 带自定义名称。"""
        ctx = _make_ctx(str(tmp_path))
        tool = CronTool.create(ctx)

        result = _run(tool.execute(action="add", name="my-job", message="Test", every_seconds=30))
        assert "my-job" in str(result)


class TestCronList:
    """list 操作。"""

    def test_list_empty(self, tmp_path: Path) -> None:
        """空任务列表。"""
        ctx = _make_ctx(str(tmp_path))
        tool = CronTool.create(ctx)

        result = _run(tool.execute(action="list"))
        assert "No cron jobs" in str(result)

    def test_list_after_add(self, tmp_path: Path) -> None:
        """添加后列出任务。"""
        ctx = _make_ctx(str(tmp_path))
        tool = CronTool.create(ctx)

        _run(tool.execute(action="add", message="Task 1", every_seconds=60))
        _run(tool.execute(action="add", message="Task 2", cron_expr="0 9 * * *"))

        result = _run(tool.execute(action="list"))
        assert "2 cron job(s)" in str(result)
        assert "Task 1" in str(result)
        assert "Task 2" in str(result)


class TestCronRemove:
    """remove 操作。"""

    def test_remove_existing(self, tmp_path: Path) -> None:
        """删除存在的任务。"""
        ctx = _make_ctx(str(tmp_path))
        tool = CronTool.create(ctx)

        add_result = _run(tool.execute(action="add", message="To remove", every_seconds=60))
        # 从结果中提取 job_id
        job_id = str(add_result).split("Created cron job ")[1].split(" ")[0]

        result = _run(tool.execute(action="remove", job_id=job_id))
        assert "Removed cron job" in str(result)

        # 验证已删除
        list_result = _run(tool.execute(action="list"))
        assert "No cron jobs" in str(list_result)

    def test_remove_missing_job_id(self, tmp_path: Path) -> None:
        """remove 缺少 job_id 报错。"""
        ctx = _make_ctx(str(tmp_path))
        tool = CronTool.create(ctx)

        result = _run(tool.execute(action="remove"))
        assert isinstance(result, ToolResult)
        assert result.is_error

    def test_remove_not_found(self, tmp_path: Path) -> None:
        """删除不存在的任务报错。"""
        ctx = _make_ctx(str(tmp_path))
        tool = CronTool.create(ctx)

        result = _run(tool.execute(action="remove", job_id="nonexistent"))
        assert isinstance(result, ToolResult)
        assert result.is_error


class TestCronValidation:
    """参数校验。"""

    def test_missing_action(self, tmp_path: Path) -> None:
        """缺少 action 报错。"""
        ctx = _make_ctx(str(tmp_path))
        tool = CronTool.create(ctx)

        result = _run(tool.execute())
        assert isinstance(result, ToolResult)
        assert result.is_error

    def test_unknown_action(self, tmp_path: Path) -> None:
        """未知 action 报错。"""
        ctx = _make_ctx(str(tmp_path))
        tool = CronTool.create(ctx)

        result = _run(tool.execute(action="update"))
        assert isinstance(result, ToolResult)
        assert result.is_error


class TestCronDiscovery:
    """工具发现。"""

    def test_discovered(self, tmp_path: Path) -> None:
        """ToolLoader 自动发现 cron。"""
        ctx = _make_ctx(str(tmp_path))
        registry = ToolRegistry()
        loaded = ToolLoader().load(ctx, registry, scope="core")

        assert "cron" in loaded
        assert registry.has("cron")

    def test_not_disabled_without_store(self, tmp_path: Path) -> None:
        """没有 cron_store 时不加载（enabled 返回 False）。"""
        from step89.tools.file_state import FileStateStore
        ctx = ToolContext(
            config=_make_config(),
            workspace=str(tmp_path),
            restrict_to_workspace=False,
            session_key="test",
            file_state_store=FileStateStore(),
            # cron_store=None
        )
        registry = ToolRegistry()
        loaded = ToolLoader().load(ctx, registry, scope="core")

        assert "cron" not in loaded

    def test_schema(self, tmp_path: Path) -> None:
        """参数 schema 正确。"""
        ctx = _make_ctx(str(tmp_path))
        tool = CronTool.create(ctx)
        schema = tool.to_schema()

        assert schema["function"]["name"] == "cron"
        props = schema["function"]["parameters"]["properties"]
        assert "action" in props
        assert "message" in props
        assert "job_id" in props
