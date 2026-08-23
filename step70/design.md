# Step 70 Design: ExecTool 增强版

## 1. 架构概览

在 step69 `tools/shell.py` 基础上增强：

```
ExecTool(Tool)
  ├── 新增配置字段
  │   ├── allowed_env_keys: list[str]   环境变量白名单
  │   ├── allow_patterns: list[str]      命令允许模式（优先）
  │   ├── deny_patterns: list[str]       命令拒绝模式
  │   ├── path_prepend: str              PATH 前缀
  │   └── path_append: str               PATH 后缀
  ├── 新增方法
  │   ├── _build_env() -> dict           构建最小化环境
  │   ├── _apply_path(env, command)      应用 PATH 修改
  │   ├── _check_command_filter(command) 灵活命令过滤
  │   └── _format_output(...)            退出码标记
  └── 修改方法
      ├── __init__: 接收 5 个新参数
      ├── create: 从配置读取 5 个新字段
      └── execute: 使用 _build_env + _check_command_filter + 退出码标记
```

## 2. 配置扩展

`config/schema.py` 的 `ExecToolConfig` 新增：

```python
class ExecToolConfig(Base):
    enable: bool = True
    timeout: int = Field(default=60, ge=0)
    sandbox: str = ""
    # step70 新增
    allowed_env_keys: list[str] = Field(default_factory=list)
    allow_patterns: list[str] = Field(default_factory=list)
    deny_patterns: list[str] = Field(default_factory=list)
    path_prepend: str = ""
    path_append: str = ""
```

## 3. 模块详细设计

### 3.1 `_build_env()`

构建最小化环境变量字典。

**Windows**：
```python
env = {
    "SYSTEMROOT": ..., "COMSPEC": ..., "USERPROFILE": ...,
    "HOMEDRIVE": ..., "HOMEPATH": ..., "TEMP": ..., "TMP": ...,
    "PATHEXT": ..., "PATH": ..., "PYTHONUNBUFFERED": "1",
    "APPDATA": ..., "LOCALAPPDATA": ..., "ProgramData": ...,
    "ProgramFiles": ..., "ProgramFiles(x86)": ..., "ProgramW6432": ...,
}
for key in self.allowed_env_keys:
    if key in os.environ: env[key] = os.environ[key]
```

**Unix**：
```python
env = {
    "HOME": ..., "LANG": ..., "TERM": ..., "PYTHONUNBUFFERED": "1",
}
for key in self.allowed_env_keys:
    if key in os.environ: env[key] = os.environ[key]
```

### 3.2 `_check_command_filter(command)`

灵活命令过滤，返回错误消息或 None。

```python
def _check_command_filter(self, command: str) -> str | None:
    lower = command.strip().lower()

    # allow 优先：匹配 allow 则直接允许
    if self.allow_patterns:
        if any(re.fullmatch(p, lower) for p in self.allow_patterns):
            return None  # 显式允许

    # deny 检查
    for pattern in self.deny_patterns:
        if re.search(pattern, lower):
            return f"Error: Command blocked by deny pattern: '{pattern}'"

    # 白名单模式：allow 非空但不匹配 → 拒绝
    if self.allow_patterns:
        return "Error: Command blocked by allowlist filter (not in allowlist)"

    return None
```

注意：`deny_patterns` 包含默认黑名单 + 配置中的额外 deny 模式。

### 3.3 PATH 管理

`_apply_path(env, command)` -> (env, command)

**Windows**：直接修改 env["PATH"]
```python
if self.path_prepend or self.path_append:
    parts = []
    if self.path_prepend: parts.append(self.path_prepend)
    if env.get("PATH"): parts.append(env["PATH"])
    if self.path_append: parts.append(self.path_append)
    env["PATH"] = os.pathsep.join(parts)
```

**Unix**：在命令前加 export 前缀
```python
if self.path_prepend or self.path_append:
    segments = []
    if self.path_prepend: segments.append(self.path_prepend)
    segments.append("$PATH")
    if self.path_append: segments.append(self.path_append)
    command = f'export PATH="{os.pathsep.join(segments)}"; {command}'
```

### 3.4 退出码标记

在 execute 中，非零退出码时输出前缀：
```python
if exit_code != 0:
    output_parts.insert(0, f"[exit code {exit_code}]")
```

## 4. 错误处理

| 场景 | 返回消息 |
|------|---------|
| deny 匹配 | `Error: Command blocked by deny pattern: '{pattern}'` |
| allowlist 不匹配 | `Error: Command blocked by allowlist filter (not in allowlist)` |
| 其他 | 同 step69 |

## 5. 测试策略

`tests/test_exec_enhanced.py`：
1. `test_build_env_minimal`：_build_env 返回最小化环境
2. `test_allowed_env_keys_passed`：白名单变量被传递
3. `test_deny_pattern_blocks`：自定义 deny 模式拦截
4. `test_allow_pattern_overrides_deny`：allow 豁免 deny
5. `test_allowlist_mode_rejects_unknown`：白名单模式拒绝未知命令
6. `test_path_prepend`：PATH 前缀生效
7. `test_path_append`：PATH 后缀生效
8. `test_nonzero_exit_code_marker`：非零退出码带前缀
9. `test_config_fields_read`：配置字段正确读取
10. `test_backward_compatibility`：step69 测试全部通过（回归）
