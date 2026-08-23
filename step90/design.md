# Step 90 Design: CronTool 真实调度

## 1. 架构

```
tools/cron.py（修改）
  ├── _CronJob（已有）
  ├── _CronStore（已有）
  ├── CronScheduler（新增）
  │   ├── start()           启动后台调度循环
  │   ├── stop()            停止调度
  │   ├── _scheduler_loop() 后台 asyncio task
  │   └── on_trigger 回调   任务触发时调用
  └── CronTool（修改）
      ├── scheduler 字段
      └── add 时注册到调度器
```

## 2. CronScheduler

```python
class CronScheduler:
    def __init__(self, on_trigger: Callable[[_CronJob], None]):
        self._store = _CronStore()
        self._task = None
        self._running = False
        self.on_trigger = on_trigger

    def start(self):
        self._running = True
        self._task = asyncio.create_task(self._scheduler_loop())

    def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()

    async def _scheduler_loop(self):
        while self._running:
            now = time.time()
            for job in list(self._store.jobs.values()):
                if job.next_run and now >= job.next_run:
                    self.on_trigger(job)
                    if job.every_seconds:
                        job.next_run = now + job.every_seconds
                    else:
                        self._store.remove(job.id)
            await asyncio.sleep(1)  # 每秒检查一次
```

## 3. _CronJob 增强

新增 `next_run` 字段（float 时间戳）：
- every_seconds 任务：add 时设置 next_run = now + every_seconds
- at 任务：next_run = at 时间戳
- cron_expr 任务：不调度（只存储）

## 4. CronTool 集成

CronTool 新增 `scheduler` 字段：
- create 时创建 CronScheduler（如果 context 提供 on_trigger 回调）
- add 任务时注册到调度器（设置 next_run）
- remove 时从调度器移除

## 5. 测试策略

- CronScheduler 启动/停止
- every_seconds 任务触发（用短间隔 + mock 回调）
- at 任务触发（设置过去时间立即触发）
- 一次性任务触发后移除
- 调度器循环不阻塞（asyncio）
