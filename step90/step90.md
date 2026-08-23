# step90：CronTool 真实调度

## 实现

修改 `tools/cron.py`：
- _CronJob 新增 next_run 字段（float 时间戳，None 表示不调度）
- _CronStore 新增 jobs 属性（调度器访问内部字典）
- CronScheduler 类：后台 asyncio task 调度器
  - start()/stop() 控制调度循环
  - 每秒检查到期任务，触发 on_trigger 回调
  - every_seconds 任务周期性触发并更新 next_run
  - at 任务一次性触发后移除
  - cron_expr 任务只存储不调度
- CronTool 集成 scheduler：create 从 context 读取，add 时设置 next_run

修改 `context.py`：
- ToolContext 新增 cron_scheduler 字段

## 文件修改清单

| 文件 | 操作 |
|------|------|
| `tools/cron.py` | 修改：+next_run +CronScheduler +scheduler集成 |
| `context.py` | 修改：+cron_scheduler字段 |
| `tests/test_cron_scheduler.py` | 新建（20测试） |
| `proposal.md`/`design.md`/`api-spec.md` | 新建 |

## 测试结果

36 passed（16旧 + 20新）
