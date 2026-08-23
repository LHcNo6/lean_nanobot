# Step 78 Proposal: CronTool 定时任务

## 1. 问题背景

agent 无法创建定时提醒和周期性任务。
nanobot 的 CronTool 支持 add/list/remove 定时任务，依赖 CronService 后台执行。

## 2. 目标

新建 `tools/cron.py`，实现简化版 CronTool：
1. add：创建定时任务（every_seconds 间隔 / cron_expr 表达式 / at 一次性）
2. list：列出所有定时任务
3. remove：删除指定任务
4. 用内存存储管理任务元数据（简化版，不实现真正的后台执行）
5. 通过 ToolContext 传递 cron_store

## 3. 非目标

- 不实现真正的后台任务执行
- 不实现 cron 表达式解析（只存储）
- 不实现持久化

## 4. 验收标准

1. CronTool 可被 ToolLoader 发现
2. add 创建任务成功
3. list 列出所有任务
4. remove 删除任务
5. 缺少必要参数时报错
6. 单元测试通过
