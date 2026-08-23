# Step 70 Proposal: ExecTool 增强版

## 1. 问题背景

step69 实现了 ExecTool 基础版，支持命令执行、超时、输出截断、危险命令黑名单、
workspace 边界检查。但存在以下不足：

1. **环境变量无管控**：子进程继承父进程全部环境变量，可能泄露 API key 等敏感信息
2. **命令过滤不灵活**：只有固定黑名单，无法按配置自定义允许/拒绝模式
3. **无法修改 PATH**：不能添加自定义工具路径（如项目本地 node_modules/.bin）
4. **退出码处理简单**：非零退出码只显示在输出中，不标记为 error

nanobot 的 ExecTool 通过 `_build_env()`、`allow_patterns`/`deny_patterns`、
`path_prepend`/`path_append` 解决这些问题。

## 2. 目标

在 step69 基础上增强 ExecTool：

1. **环境变量白名单**：`_build_env()` 构建最小化环境，`allowed_env_keys` 控制额外传递的变量
2. **灵活命令过滤**：`allow_patterns`（白名单，优先）+ `deny_patterns`（黑名单，可配置）
3. **PATH 管理**：`path_prepend`/`path_append` 修改子进程 PATH
4. **配置扩展**：`ExecToolConfig` 添加 5 个新字段
5. **退出码标记**：非零退出码时输出前缀标记（不改变返回类型，保持向后兼容）

## 3. 非目标

- **不实现** shell 选择（bash/sh/zsh/cmd/powershell）—— 保持系统默认 shell
- **不实现** login shell 模式
- **不实现** 沙箱包装（bwrap/macOS sandbox）
- **不实现** 内部 URL 检测（需要 network 模块，依赖过重）
- **不实现** 绝对路径提取和 workspace 内路径检查（step69 已有 working_dir 边界检查）
- **不实现** 交互式会话（yield_time_ms）—— step73

## 4. 关键设计决策

### 4.1 环境变量策略
- **Windows**：传递系统必需变量（SYSTEMROOT/COMSPEC/PATH/TEMP 等）+ allowed_env_keys
- **Unix**：仅传递 HOME/LANG/TERM/PYTHONUNBUFFERED + allowed_env_keys
- 目的：最小化环境变量，减少敏感信息泄露

### 4.2 allow/deny 过滤优先级
```
if allow_patterns 且命令匹配任意 allow → 允许（跳过 deny 检查）
elif 命令匹配任意 deny → 拒绝
elif allow_patterns 且命令不匹配任何 allow → 拒绝（白名单模式）
else → 允许
```
allow 优先于 deny，用户可以通过 allow_patterns 豁免特定命令。

### 4.3 PATH 管理
- Windows：直接修改 env["PATH"] = prepend + 原PATH + append
- Unix：在命令前加 `export PATH="..."` 前缀（因为 shell 会读取 PATH）

### 4.4 退出码标记
非零退出码时，输出第一行加 `[exit code N]` 前缀，让 agent 更容易识别失败。
不改变返回类型（仍返回 str，不是 ToolResult.error），因为非零退出码不一定是错误
（如 grep 无匹配返回 1）。

## 5. 验收标准

1. `_build_env()` 返回最小化环境变量，不包含未授权的敏感变量
2. `allowed_env_keys` 中的变量被传递给子进程
3. `deny_patterns` 匹配的命令被拦截
4. `allow_patterns` 匹配的命令被豁免（即使匹配 deny）
5. `allow_patterns` 非空时，不匹配的命令被拒绝（白名单模式）
6. `path_prepend`/`path_append` 修改子进程 PATH
7. 配置中 5 个新字段正确读取
8. 非零退出码输出带前缀标记
9. step69 所有测试通过（向后兼容）
10. 新增增强功能单元测试
