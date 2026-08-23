# Step 90 Proposal: CronTool 真实调度

## 1. 问题背景

step78 的 CronTool 只管理任务元数据（add/list/remove），没有真正的调度执行。
nanobot 的 CronService 有后台调度器，在任务触发时执行回调（发送消息给 agent）。

## 2. 目标

增强 `tools/cron.py`：
1. CronScheduler 类：后台 asyncio task 调度器
2. 支持 every_seconds 间隔任务（周期性触发）
3. 支持 at 一次性任务（指定时间触发）
4. 任务触发时调用回调函数（on_trigger）
5. 调度器可以启动/停止
6. CronTool 集成调度器

## 3. 非目标

- 不实现 cron 表达式解析（cron_expr 只存储不调度）
- 不实现任务持久化
- 不实现任务执行结果记录

## 4. 验收标准

1. CronScheduler 可以启动和停止
2. every_seconds 任务按间隔触发回调
3. at 任务在指定时间触发回调
4. 触发后一次性任务被移除
5. CronTool 集成调度器
6. 单元测试通过（用 mock 回调验证触发）
