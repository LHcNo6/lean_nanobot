"""step90：CronTool 真实调度单元测试。"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from step102.tools.cron import (
    CronScheduler,
    CronTool,
    _CronJob,
    _CronStore,
)


def _run(coro):
    return asyncio.run(coro)


class TestCronJobNextRun:
    """_CronJob next_run 字段。"""

    def test_default_none(self) -> None:
        """默认 next_run 为 None。"""
        job = _CronJob(job_id="1", name="test", message="hello")
        assert job.next_run is None

    def test_can_set(self) -> None:
        """可以设置 next_run。"""
        job = _CronJob(job_id="1", name="test", message="hello", next_run=1234567890.0)
        assert job.next_run == 1234567890.0


class TestCronStoreJobsProperty:
    """_CronStore.jobs 属性。"""

    def test_jobs_property(self) -> None:
        """jobs 属性返回内部字典。"""
        store = _CronStore()
        job = _CronJob(job_id="1", name="test", message="hello")
        store.add(job)
        assert "1" in store.jobs
        assert store.jobs["1"] is job


class TestCronScheduler:
    """CronScheduler。"""

    def test_init(self) -> None:
        """初始化。"""
        scheduler = CronScheduler()
        assert not scheduler.running
        assert scheduler.store is not None
        assert scheduler.on_trigger is None

    def test_init_with_callback(self) -> None:
        """初始化带回调。"""
        callback = MagicMock()
        scheduler = CronScheduler(on_trigger=callback)
        assert scheduler.on_trigger is callback

    def test_start_stop(self) -> None:
        """启动和停止。"""
        scheduler = CronScheduler(check_interval=0.01)

        async def _test():
            scheduler.start()
            assert scheduler.running
            await asyncio.sleep(0.05)
            scheduler.stop()
            assert not scheduler.running

        _run(_test())

    def test_double_start_no_error(self) -> None:
        """重复启动不报错。"""
        scheduler = CronScheduler(check_interval=0.01)

        async def _test():
            scheduler.start()
            scheduler.start()  # 不应报错
            assert scheduler.running
            scheduler.stop()

        _run(_test())

    def test_stop_without_start_no_error(self) -> None:
        """未启动时停止不报错。"""
        scheduler = CronScheduler()
        scheduler.stop()  # 不应报错

    def test_every_seconds_triggers_callback(self) -> None:
        """every_seconds 任务触发回调。"""
        callback = MagicMock()
        scheduler = CronScheduler(on_trigger=callback, check_interval=0.01)

        # 添加一个 0.05 秒后触发的任务
        job = _CronJob(
            job_id="1", name="test", message="hello",
            every_seconds=0.05, next_run=time.time() + 0.05,
        )
        scheduler.store.add(job)

        async def _test():
            scheduler.start()
            await asyncio.sleep(0.2)
            scheduler.stop()

        _run(_test())
        assert callback.called
        assert callback.call_args[0][0].job_id == "1"

    def test_at_triggers_callback(self) -> None:
        """at 任务（过去时间）立即触发。"""
        callback = MagicMock()
        scheduler = CronScheduler(on_trigger=callback, check_interval=0.01)

        # 添加一个已经到期的任务
        job = _CronJob(
            job_id="2", name="test", message="hello",
            at="2020-01-01T00:00:00", next_run=time.time() - 1,
        )
        scheduler.store.add(job)

        async def _test():
            scheduler.start()
            await asyncio.sleep(0.1)
            scheduler.stop()

        _run(_test())
        assert callback.called

    def test_one_time_job_removed_after_trigger(self) -> None:
        """一次性任务触发后被移除。"""
        callback = MagicMock()
        scheduler = CronScheduler(on_trigger=callback, check_interval=0.01)

        job = _CronJob(
            job_id="3", name="test", message="hello",
            at="2020-01-01T00:00:00", next_run=time.time() - 1,
        )
        scheduler.store.add(job)

        async def _test():
            scheduler.start()
            await asyncio.sleep(0.1)
            scheduler.stop()

        _run(_test())
        assert scheduler.store.get("3") is None

    def test_recurring_job_not_removed(self) -> None:
        """周期性任务触发后不被移除，更新 next_run。"""
        callback = MagicMock()
        scheduler = CronScheduler(on_trigger=callback, check_interval=0.01)

        job = _CronJob(
            job_id="4", name="test", message="hello",
            every_seconds=0.05, next_run=time.time() + 0.05,
        )
        scheduler.store.add(job)

        async def _test():
            scheduler.start()
            await asyncio.sleep(0.15)
            scheduler.stop()

        _run(_test())
        # 周期性任务应该还在
        assert scheduler.store.get("4") is not None
        # next_run 应该被更新
        assert scheduler.store.get("4").next_run is not None

    def test_no_trigger_for_future_job(self) -> None:
        """未来任务不触发。"""
        callback = MagicMock()
        scheduler = CronScheduler(on_trigger=callback, check_interval=0.01)

        job = _CronJob(
            job_id="5", name="test", message="hello",
            every_seconds=100, next_run=time.time() + 100,
        )
        scheduler.store.add(job)

        async def _test():
            scheduler.start()
            await asyncio.sleep(0.1)
            scheduler.stop()

        _run(_test())
        assert not callback.called

    def test_callback_exception_does_not_crash(self) -> None:
        """回调异常不影响调度器。"""
        def bad_callback(job):
            raise RuntimeError("boom")

        scheduler = CronScheduler(on_trigger=bad_callback, check_interval=0.01)
        job = _CronJob(
            job_id="6", name="test", message="hello",
            at="2020-01-01T00:00:00", next_run=time.time() - 1,
        )
        scheduler.store.add(job)

        async def _test():
            scheduler.start()
            await asyncio.sleep(0.1)
            scheduler.stop()  # 不应抛异常

        _run(_test())  # 不应抛异常


class TestCronToolSchedulerIntegration:
    """CronTool 调度器集成。"""

    def _make_ctx(self, scheduler=None):
        from step102.tools.file_state import FileStateStore
        from step102.context import ToolContext
        cfg = SimpleNamespace(
            exec=SimpleNamespace(enable=True, timeout=60, sandbox="", allowed_env_keys=[], allow_patterns=[], deny_patterns=[], path_prepend="", path_append=""),
            tools=SimpleNamespace(restrict_to_workspace=False, file=SimpleNamespace(enable=True)),
            web=SimpleNamespace(enable=True, timeout=30, user_agent="Test", search=SimpleNamespace(provider="duckduckgo", max_results=5, timeout=30)),
            my=SimpleNamespace(enable=True, allow_set=False),
            image_generation=SimpleNamespace(enabled=True, provider="simple", save_dir="generated"),
        )
        return ToolContext(
            config=cfg, workspace="C:/tmp", restrict_to_workspace=False,
            session_key="test", file_state_store=FileStateStore(),
            cron_store=scheduler.store if scheduler else _CronStore(),
            cron_scheduler=scheduler,
        )

    def test_create_with_scheduler(self) -> None:
        """create 时传入 scheduler。"""
        scheduler = CronScheduler()
        ctx = self._make_ctx(scheduler=scheduler)
        tool = CronTool.create(ctx)
        assert tool._scheduler is scheduler

    def test_create_without_scheduler(self) -> None:
        """create 时不传 scheduler。"""
        ctx = self._make_ctx(scheduler=None)
        tool = CronTool.create(ctx)
        assert tool._scheduler is None

    def test_add_every_seconds_sets_next_run(self) -> None:
        """add every_seconds 任务设置 next_run。"""
        scheduler = CronScheduler()
        ctx = self._make_ctx(scheduler=scheduler)
        tool = CronTool.create(ctx)

        result = _run(tool.execute(action="add", message="test", every_seconds=60))
        assert "Created" in str(result)

        jobs = scheduler.store.list()
        assert len(jobs) == 1
        assert jobs[0].next_run is not None
        assert jobs[0].next_run > time.time()

    def test_add_at_sets_next_run(self) -> None:
        """add at 任务设置 next_run。"""
        scheduler = CronScheduler()
        ctx = self._make_ctx(scheduler=scheduler)
        tool = CronTool.create(ctx)

        result = _run(tool.execute(action="add", message="test", at="2030-01-01T00:00:00"))
        assert "Created" in str(result)

        jobs = scheduler.store.list()
        assert len(jobs) == 1
        assert jobs[0].next_run is not None

    def test_add_cron_expr_no_next_run(self) -> None:
        """add cron_expr 任务不设置 next_run（不调度）。"""
        scheduler = CronScheduler()
        ctx = self._make_ctx(scheduler=scheduler)
        tool = CronTool.create(ctx)

        result = _run(tool.execute(action="add", message="test", cron_expr="0 9 * * *"))
        assert "Created" in str(result)

        jobs = scheduler.store.list()
        assert len(jobs) == 1
        assert jobs[0].next_run is None

    def test_add_invalid_at_no_next_run(self) -> None:
        """add 无效 at 不设置 next_run。"""
        scheduler = CronScheduler()
        ctx = self._make_ctx(scheduler=scheduler)
        tool = CronTool.create(ctx)

        result = _run(tool.execute(action="add", message="test", at="invalid-date"))
        assert "Created" in str(result)

        jobs = scheduler.store.list()
        assert len(jobs) == 1
        assert jobs[0].next_run is None
