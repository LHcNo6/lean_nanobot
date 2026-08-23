# Step 78 Design: CronTool

## 1. 架构

```
tools/cron.py（新建）
  ├── _CronJob          定时任务数据类
  ├── _CronStore        内存任务存储
  └── CronTool(Tool)    定时任务工具
```

## 2. 参数

```python
action: "add" | "list" | "remove"  # 必填
name: str                           # 任务名称（可选）
message: str                        # add 时必填，触发时执行的指令
every_seconds: int = 0              # 间隔秒数
cron_expr: str                      # cron 表达式
at: str                             # ISO  datetime（一次性）
tz: str                             # 时区
job_id: str                         # remove 时必填
```

## 3. 执行流程

1. 校验 action
2. add：校验 message + 至少一个调度参数，生成 job_id，存储
3. list：返回所有任务
4. remove：校验 job_id，从存储删除

## 4. 任务存储

_CronStore 用 dict 存储，key=job_id，value=_CronJob。
通过 ToolContext.cron_store 传递。

## 5. 测试策略

- add 任务
- list 任务
- remove 任务
- add 缺少 message
- remove 缺少 job_id
- remove 不存在的 job_id
- 工具发现
