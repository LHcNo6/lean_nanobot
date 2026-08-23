# Step 84 API Specification

## 1. ExecSessionInfo 数据类

**文件**：`tools/exec_session.py`

```python
@dataclass
class ExecSessionInfo:
    session_id: str
    command: str
    cwd: str
    elapsed_s: float
    status: str          # "running" / "exited"
    returncode: int | None
```

## 2. ExecSessionManager.list()

```python
def list(self) -> list[ExecSessionInfo]
```

列出所有会话（包括已退出但未清理的）。

返回会话摘要列表，按启动时间排序。

## 3. ListExecSessionsTool API

### 工具元数据

| 属性 | 值 |
|------|-----|
| name | `list_exec_sessions` |
| config_key | `exec` |
| read_only | True |
| _scopes | {"core"} |

### 工具参数

无参数。

### 输出格式

每个会话一行：
```
{session_id} | {status} | elapsed={elapsed_s:.1f}s | cwd={cwd} | {command}
```

命令超过 120 字符时截断为 119 字符 + "..."。

无会话时返回：
```
No active exec sessions.
```

### create()

从上下文创建，使用 `ctx.exec_session_manager`，为 None 时创建默认管理器。
