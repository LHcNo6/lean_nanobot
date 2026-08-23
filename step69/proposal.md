# Step 69 Proposal: ExecTool 基础版

## 1. 问题背景

step65-68 完成了文件系统工具（write/edit/list/find/grep），agent 可以操作文件了。
但 agent 无法执行 shell 命令——不能运行测试、构建项目、安装依赖、执行 git 命令。
这是 agent 能力的重大缺失。

nanobot 的 `ExecTool`（`tools/shell.py`，33KB）提供完整的 shell 执行能力，包含
命令守卫、沙箱、环境变量管理、交互式会话等。step69 以最小增量引入基础版
ExecTool，覆盖最常用的同步命令执行场景。

## 2. 目标

新建 `tools/shell.py`，实现 `ExecTool` 基础版：

1. 核心参数：`command`（必填）、`working_dir`（可选）、`timeout`（可选）
2. 使用 `asyncio.create_subprocess_shell` 执行命令
3. 超时控制（默认 60s，最大 600s），超时杀死进程
4. 输出截断（默认 10000 字符，头尾保留）
5. 显示退出码
6. 基本危险命令黑名单（rm -rf、format、mkfs、dd、shutdown、reboot、fork bomb）
7. workspace 边界检查（restrict_to_workspace 时 working_dir 不能越界）
8. 配置集成：`config.exec.enable`、`config.exec.timeout`

## 3. 非目标（明确不做）

- **不实现** 交互式会话（yield_time_ms / session_id）—— step73
- **不实现** 环境变量白名单/黑名单管理 —— step70
- **不实现** allow_patterns/deny_patterns 灵活命令过滤 —— step70
- **不实现** 沙箱包装（bwrap/macOS sandbox）—— 远期
- **不实现** shell 选择（bash/sh/zsh/cmd/powershell）—— 用系统默认 shell
- **不实现** login shell 模式 —— 远期
- **不实现** path_prepend/path_append —— step70
- **不实现** 流式输出 —— 一次性返回完整输出

## 4. 方案选择

### 方案 A：`os.system` / `subprocess.run`（同步）
- 优点：简单
- 缺点：阻塞事件循环，无法与 async agent 循环配合

### 方案 B：`asyncio.create_subprocess_shell`（选定）
- 优点：异步非阻塞，与 agent 循环配合，支持超时
- 缺点：比同步方案稍复杂

**选择方案 B**。agent 主循环是 async 的，工具执行必须非阻塞。

### 命令执行方式
- Unix：`asyncio.create_subprocess_shell(command, cwd=cwd, env=env)`
- Windows：同样用 `create_subprocess_shell`（Python 在 Windows 上用 cmd.exe）

## 5. 关键设计决策

### 5.1 危险命令黑名单
```python
_DEFAULT_DENY_PATTERNS = [
    r"\brm\s+-[rf]{1,2}\b",       # rm -r, rm -rf
    r"\bformat\b",                   # format
    r"\b(mkfs|diskpart)\b",         # 磁盘操作
    r"\bdd\s+if=",                   # dd
    r"\b(shutdown|reboot|poweroff)\b",  # 系统电源
    r":\(\)\s*\{.*\};\s*:",          # fork bomb
]
```
使用 `re.search` 检测，匹配到则返回错误，不执行。这是基础安全网，
防止 agent 误执行破坏性命令。

### 5.2 输出截断策略
```python
if len(result) > max_output:
    half = max_output // 2
    result = result[:half] + f"\n... ({truncated} chars truncated) ...\n" + result[-half:]
```
头尾各保留一半，中间省略。这样既能看到命令开头的输出，也能看到结尾的错误信息。

### 5.3 workspace 边界检查
当 `restrict_to_workspace=True` 时：
- `working_dir` 必须在 workspace 内；
- 越界则返回错误，不执行。

这防止 agent 通过 `working_dir="/etc"` 绕过 workspace 限制。

### 5.4 超时处理
```python
try:
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
except asyncio.TimeoutError:
    process.kill()
    await process.wait()
    return ToolResult.error(f"Error: Command timed out after {timeout} seconds")
```
超时后杀死进程并等待回收，避免僵尸进程。

## 6. 验收标准

1. `ExecTool` 可被 `ToolLoader` 自动发现并注册
2. 简单命令（如 `echo hello`）执行成功，返回输出
3. 非零退出码的命令返回错误输出和退出码
4. 超时命令被杀死并返回超时错误
5. 危险命令（如 `rm -rf /`）被拦截
6. workspace 受限模式下越界 working_dir 被拒绝
7. 长输出被截断
8. `config.exec.enable=False` 时工具不被加载
9. 所有现有测试通过，新增 ExecTool 单元测试
