"""异步取消辅助（对齐 nanobot ``utils/cancellation.py``）。

A13：``asyncio.CancelledError`` 可能由外部集成层泄漏进主循环（某些库在
内部作用域抛 CancelledError 而非真正取消当前任务）。``task_is_cancelling``
区分"当前任务真的在被取消"（``Task.cancelling() > 0``）与"泄漏的假
取消信号"，供消费循环决定 raise 还是吞掉继续跑。
"""

from __future__ import annotations

import asyncio


def task_is_cancelling() -> bool:
    """当前 asyncio 任务是否处于真正被取消的状态。

    Returns:
        True 表示当前任务已收到取消请求（外部 `task.cancel()` 或
        `asyncio.wait_for` 超时）；False 表示不存在当前任务，或任务
        没有被取消（此时抛出的 CancelledError 属泄漏信号，应忽略）。
    """
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0