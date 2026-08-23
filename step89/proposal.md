# Step 89 Proposal: ExecTool 沙箱后端抽象

## 1. 问题背景

当前 ExecTool 直接执行 shell 命令，没有沙箱隔离。nanobot 支持 bwrap 等沙箱后端，
通过 `sandbox.py` 的函数注册机制包装命令，限制命令对文件系统的访问。

## 2. 目标

新建 `tools/sandbox.py`，实现沙箱后端抽象：
1. 函数注册机制（对齐 nanobot 的 _BACKENDS 字典）
2. `none` 后端：不包装（默认）
3. `bwrap` 后端：bubblewrap 沙箱（Linux，对齐 nanobot）
4. `wrap_command(sandbox, command, workspace, cwd)` 入口函数
5. ExecTool 集成：根据 config.exec.sandbox 选择后端包装命令

## 3. 非目标

- 不实现 firejail/docker 等其他沙箱
- 不实现 Windows 沙箱（bwrap 是 Linux only）
- 不实现沙箱可用性检测

## 4. 验收标准

1. sandbox.py 有 wrap_command 函数
2. none 后端返回原命令
3. bwrap 后端生成正确的 bwrap 命令
4. 未知沙箱名报错
5. ExecTool 在 sandbox 非空时调用 wrap_command
6. 单元测试通过
