# Step 90 API Specification

## 1. CronScheduler API

**文件**：`tools/cron.py`

### CronScheduler.__init__()

```python
def __init__(self, on_trigger: Callable[["_CronJob"], None] | None = None)
```

创建调度器。

| 参数 | 类型 | 说明 |
|------|------|------|
| `on_trigger` | Callable | 任务触发时的回调函数，接收 _CronJob 参数 |

### CronScheduler.start()

```python
def start(self) -> None
```

启动后台调度循环（asyncio task）。重复调用不报错。

### CronScheduler.stop()

```python
def stop(self) -> None
```

停止调度循环。

### CronScheduler.store

```python
@property
def store(self) -> _CronStore
```

访问内部的 _CronStore。

## 2. _CronJob 增强

新增字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `next_run` | float \| None | 下次触发时间戳（None 表示不调度） |

## 3. CronTool 集成

CronTool 新增：

| 字段 | 类型 | 说明 |
|------|------|------|
| `scheduler` | CronScheduler \| None | 调度器实例（可选） |

add 任务时：
- 如果有 scheduler，设置 next_run 并注册到调度器
- every_seconds 任务：next_run = now + every_seconds
- at 任务：next_run = at 时间戳
- cron_expr 任务：next_run = None（不调度）
