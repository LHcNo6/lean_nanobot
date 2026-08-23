# step70：ExecTool 增强版（环境变量 + 命令过滤 + PATH 管理）

## 1. 问题背景

step69 实现了 ExecTool 基础版，但存在不足：
1. 环境变量无管控：子进程继承父进程全部环境变量，可能泄露 API key
2. 命令过滤不灵活：只有固定黑名单，无法按配置自定义
3. 无法修改 PATH：不能添加项目本地工具路径
4. 退出码处理简单：非零退出码只显示在输出中

## 2. 实现方案

### 2.1 环境变量白名单（`_build_env`）
- Windows：系统必需变量（SYSTEMROOT/COMSPEC/PATH/TEMP 等 17 个）+ allowed_env_keys
- Unix：HOME/LANG/TERM/PYTHONUNBUFFERED + allowed_env_keys
- 目的：最小化环境，减少敏感信息泄露

### 2.2 灵活命令过滤（`_check_command_filter`）
- allow_patterns（fullmatch，优先）：匹配则直接允许，跳过 deny
- deny_patterns（search，默认黑名单 + 配置额外）：匹配则拒绝
- 白名单模式：allow 非空但不匹配 → 拒绝

### 2.3 PATH 管理（`_apply_path`）
- Windows：直接修改 env["PATH"] = prepend + 原PATH + append
- Unix：命令前加 `export PATH="..."` 前缀

### 2.4 非零退出码标记
- 输出第一行加 `[exit code N]` 前缀
- 末尾仍保留 `Exit code: N`

## 3. 配置扩展

`ExecToolConfig` 新增 5 个字段：
- `allowed_env_keys: list[str]`
- `allow_patterns: list[str]`
- `deny_patterns: list[str]`
- `path_prepend: str`
- `path_append: str`

## 4. 文件修改清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `config/schema.py` | 修改 | ExecToolConfig +5 字段 |
| `tools/shell.py` | 重写 | 增强版 ExecTool |
| `tests/test_exec_enhanced.py` | 新建 | 31 个增强测试 |
| `tests/test_exec.py` | 修改 | 适配 step70 方法名/消息 |
| `proposal.md` / `design.md` / `api-spec.md` | 新建 | 规范文档 |

## 5. 测试结果

- `test_exec_enhanced.py`：31 passed
- `test_exec.py`（step69 复用）：33 passed
- **合计：64 passed**

## 6. 暴露的技术债

1. 无 shell 选择（bash/sh/zsh/cmd/powershell）
2. 无 login shell 模式
3. 无沙箱包装
4. 无内部 URL 检测
5. 无绝对路径提取和 workspace 内路径检查
6. 无交互式会话（step73）
