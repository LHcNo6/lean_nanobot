# Step 84 Design: ListExecSessionsTool

## 1. 架构

```
tools/exec_session.py（修改）
  ├── ExecSessionInfo（新增）    会话摘要数据类
  ├── ExecSessionManager（修改）
  │   └── list()                新增：返回会话摘要列表
  └── ListExecSessionsTool（新增）
      ├── create(ctx)           从上下文获取 exec_session_manager
      └── execute()             列出会话
```

## 2. ExecSessionInfo

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

## 3. ExecSessionManager.list()

遍历 _sessions 字典，为每个会话生成 ExecSessionInfo：
- elapsed_s = time.monotonic() - started_at
- status = "exited" if process.returncode is not None else "running"
- returncode = process.returncode

已退出的会话也包含在列表中（直到被清理）。

## 4. ListExecSessionsTool

```python
@tool_parameters(tool_parameters_schema())  # 无参数
class ListExecSessionsTool(Tool):
    name = "list_exec_sessions"
    config_key = "exec"
    read_only = True
```

输出格式（对齐 nanobot）：
```
session_id | status | elapsed=Xs | cwd=... | command...
```

无会话时返回 "No active exec sessions."

## 5. 测试策略

- ExecSessionInfo 数据类
- ExecSessionManager.list() 空列表
- ExecSessionManager.list() 有会话（mock _ExecSession）
- ListExecSessionsTool 无会话
- ListExecSessionsTool 有会话
- 命令过长截断
