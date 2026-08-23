# step84：ListExecSessionsTool（列出执行会话）

## 实现

修改 `tools/exec_session.py`：
- 新增 ExecSessionInfo 数据类（session_id/command/cwd/elapsed_s/status/returncode）
- ExecSessionManager 新增 list() 方法，返回会话摘要列表
- 新增 ListExecSessionsTool 工具类，列出活跃执行会话
- 输出格式对齐 nanobot：session_id | status | elapsed=Xs | cwd=... | command
- 命令超过120字符截断
- 无会话时返回 "No active exec sessions."

## 文件修改清单

| 文件 | 操作 |
|------|------|
| `tools/exec_session.py` | 修改：+ExecSessionInfo +manager.list() +ListExecSessionsTool |
| `tests/test_list_exec_sessions.py` | 新建（19测试） |
| `proposal.md`/`design.md`/`api-spec.md` | 新建 |

## 测试结果

35 passed（16旧 + 19新）
