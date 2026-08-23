# step78：CronTool 定时任务

## 实现

新建 `tools/cron.py`：
- add：创建定时任务（every_seconds / cron_expr / at）
- list：列出所有定时任务
- remove：删除指定任务
- _CronStore 内存存储
- context.py 新增 cron_store 字段

## 文件修改清单

| 文件 | 操作 |
|------|------|
| `context.py` | 修改：+cron_store 字段 |
| `tools/cron.py` | 新建 |
| `tests/test_cron.py` | 新建（16测试） |
| `proposal.md`/`design.md`/`api-spec.md` | 新建 |

## 测试结果

16 passed
