# Step 78 API Specification

## 1. CronTool API

**文件**：`tools/cron.py`
**继承**：`Tool`

| 属性 | 值 |
|------|-----|
| `name` | `"cron"` |
| `_scopes` | `{"core"}` |

### 参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `action` | string | 是 | `"add"`, `"list"`, `"remove"` |
| `name` | string | 否 | 任务名称 |
| `message` | string | add时 | 触发时执行的指令 |
| `every_seconds` | integer | 否 | 间隔秒数 |
| `cron_expr` | string | 否 | cron 表达式 |
| `at` | string | 否 | ISO datetime（一次性） |
| `tz` | string | 否 | 时区 |
| `job_id` | string | remove时 | 要删除的任务 ID |

### 返回值

add：`"Created cron job {job_id}: {name}"`
list：任务列表文本
remove：`"Removed cron job {job_id}"`
失败：`ToolResult.error(...)`

## 2. 工具发现契约

`ToolLoader` 扫描 `tools/cron.py` 时发现 `CronTool`。
最终注册：`cron`
