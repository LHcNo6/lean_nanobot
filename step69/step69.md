# step69：ExecTool 基础版（Shell 命令执行）

## 1. 问题背景

step65-68 完成了文件系统工具，agent 可以操作文件了。但 agent 无法执行 shell
命令——不能运行测试、构建项目、安装依赖、执行 git 命令。这是 agent 能力的
重大缺失。

nanobot 的 `ExecTool`（`tools/shell.py`，33KB）提供完整的 shell 执行能力，包含
命令守卫、沙箱、环境变量管理、交互式会话等。step69 以最小增量引入基础版
ExecTool，覆盖最常用的同步命令执行场景。

## 2. 原理分析

### 2.1 为什么用 `asyncio.create_subprocess_shell` 而不是 `subprocess.run`？

agent 主循环是 async 的，工具执行必须非阻塞。`subprocess.run` 会阻塞事件循环，
导致 agent 无法处理其他消息。`asyncio.create_subprocess_shell` 是异步的，
与 agent 循环配合，且原生支持 `asyncio.wait_for` 超时控制。

### 2.2 为什么需要危险命令黑名单？

agent 可能误执行破坏性命令（如 `rm -rf /`、`format C:`）。黑名单是基础安全网，
使用正则匹配检测，匹配到则拦截不执行。这比完全禁止 shell 更灵活，允许正常的
构建、测试、git 命令。

### 2.3 为什么需要 workspace 边界检查？

当 `restrict_to_workspace=True` 时，agent 不应能访问 workspace 外的文件系统。
如果不检查 `working_dir`，agent 可以通过 `working_dir="/etc"` 绕过限制，
然后执行任意命令访问系统文件。边界检查确保 cwd 始终在 workspace 内。

### 2.4 为什么输出截断用头尾保留策略？

命令输出可能很长（如编译日志、测试输出）。头尾保留策略：
- 头部：通常包含命令开始的重要信息（如编译配置、测试发现）；
- 尾部：通常包含错误信息和退出状态；
- 中间：通常是重复的进度信息，可以省略。

这比只保留头部更合理，因为错误信息通常在尾部。

### 2.5 为什么超时后要 kill 并 wait？

`asyncio.TimeoutError` 只取消了 `wait_for`，子进程仍在运行。必须：
1. `process.kill()` 发送 SIGKILL；
2. `process.wait()` 等待进程退出，回收资源，避免僵尸进程。

## 3. 实现方案

### 3.1 ExecTool 类

继承 `Tool`，参数：`command`（必填）、`working_dir`（可选）、`timeout`（可选）。

执行流程：
1. 参数校验（空 command → 错误）
2. 危险命令检查（匹配黑名单 → 错误）
3. 解析工作目录（调用参数 > 实例默认 > os.getcwd）
4. workspace 边界检查（受限模式下越界 → 错误）
5. 解析超时（调用参数不超过 600s，0=不限制）
6. 创建子进程（asyncio.create_subprocess_shell）
7. 等待完成（asyncio.wait_for，超时 kill）
8. 组装输出（stdout + STDERR + Exit code）
9. 输出截断（超过 10000 字符时头尾保留）
10. 返回结果

### 3.2 危险命令黑名单

```python
_DEFAULT_DENY_PATTERNS = [
    r"\brm\s+-[rf]{1,2}\b",          # rm -r, rm -rf
    r"\bformat\b",                      # format
    r"\b(mkfs|diskpart)\b",            # 磁盘操作
    r"\bdd\s+if=",                      # dd
    r">\s*/dev/sd",                     # 写磁盘
    r"\b(shutdown|reboot|poweroff)\b", # 系统电源
    r":\(\)\s*\{.*\};\s*:",             # fork bomb
]
```

### 3.3 配置集成

`config/schema.py` 中已有 `ExecToolConfig`：
- `enable: bool = True`
- `timeout: int = 60`
- `sandbox: str = ""`

`ExecTool.enabled(ctx)` 读取 `ctx.config.exec.enable`。
`ExecTool.create(ctx)` 读取 `ctx.config.exec.timeout` 和 `ctx.config.tools.restrict_to_workspace`。

## 4. 核心类/函数说明

### `ExecTool`

Shell 命令执行工具（基础版）。

关键方法：
- `execute(command, working_dir, timeout)`：主执行方法（async）；
- `_check_dangerous(command)`：危险命令检查；
- `_resolve_cwd(working_dir)`：解析工作目录；
- `_check_workspace_boundary(cwd)`：workspace 边界检查；
- `_resolve_timeout(timeout)`：解析有效超时；
- `_truncate_output(text, max_len)`：输出截断（头尾保留）；
- `_kill_process(process)`：杀死子进程并等待回收。

## 5. 文件修改清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `tools/shell.py` | 新建 | ExecTool 基础版 + 危险命令黑名单 |
| `tests/test_exec.py` | 新建 | 33 个单元测试 |
| `proposal.md` | 新建 | 需求定义 |
| `design.md` | 新建 | 架构设计 |
| `api-spec.md` | 新建 | 接口契约 |

## 6. 测试结果

- `tests/test_exec.py`：33 passed
  - 基础执行：5（echo/非零退出/无输出/空command/None）
  - 超时：4（kill/自定义/最大值截断/0=不限制）
  - 危险命令：7（rm -rf/format/shutdown/reboot/安全命令/普通rm/黑名单非空）
  - working_dir：2（指定/默认workspace）
  - workspace边界：3（越界拒绝/内允许/非受限允许越界）
  - 输出截断：3（长输出截断/短输出不截断/头尾保留）
  - stderr：1
  - 工具发现：4（发现/schema/非只读/名称）
  - 配置：4（禁用/启用/timeout使用/enabled类方法）

## 7. 暴露的技术债

1. **无环境变量管理**：命令继承父进程环境，无法白名单/黑名单过滤。step70 实现。
2. **无 allow/deny 灵活命令过滤**：只有固定黑名单，无法按配置自定义。step70 实现。
3. **无 shell 选择**：Windows 用 cmd.exe，Unix 用 /bin/sh，无法选择 bash/zsh/powershell。
4. **无交互式会话**：长运行命令会阻塞直到超时，无法后台运行+轮询。step73 实现。
5. **无沙箱**：命令直接在宿主机执行，无隔离。远期实现。
6. **无 path_prepend/append**：无法修改 PATH。step70 实现。
7. **Windows asyncio transport 警告**：测试中有 unclosed transport 警告，不影响功能但可优化。

## 8. 下一步

step70：ExecTool 增强版
- 环境变量白名单/黑名单管理
- allow_patterns/deny_patterns 灵活命令过滤
- path_prepend/path_append
- 更完善的退出码处理（非零退出码是否返回 error）
