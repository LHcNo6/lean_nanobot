# step89：ExecTool 沙箱后端抽象

## 实现

新建 `tools/sandbox.py`：
- 函数注册机制（_BACKENDS 字典）
- none 后端：不包装
- bwrap 后端：bubblewrap 沙箱（对齐 nanobot）
- wrap_command 入口函数
- available_backends 辅助函数

修改 `tools/shell.py`：
- ExecTool 新增 sandbox 参数
- create 方法读取 config.exec.sandbox
- 执行命令前调用 wrap_command 包装

## 文件修改清单

| 文件 | 操作 |
|------|------|
| `tools/sandbox.py` | 新建 |
| `tools/shell.py` | 修改：+sandbox参数 +集成wrap_command |
| `tests/test_sandbox.py` | 新建（17测试） |
| `proposal.md`/`design.md`/`api-spec.md` | 新建 |

## 测试结果

17 passed
