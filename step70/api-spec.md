# Step 70 API Specification

## 1. 配置扩展

`config/schema.py` 的 `ExecToolConfig` 新增字段：

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `allowed_env_keys` | list[str] | `[]` | 允许传递给子进程的额外环境变量名 |
| `allow_patterns` | list[str] | `[]` | 命令允许正则（fullmatch，优先于 deny） |
| `deny_patterns` | list[str] | `[]` | 额外命令拒绝正则（search，追加到默认黑名单） |
| `path_prepend` | str | `""` | PATH 前缀（冒号/分号分隔） |
| `path_append` | str | `""` | PATH 后缀 |

## 2. ExecTool 新增方法

### 2.1 `_build_env()`

```python
def _build_env(self) -> dict[str, str]
```
构建最小化环境变量字典。
- Windows：系统必需变量 + allowed_env_keys
- Unix：HOME/LANG/TERM/PYTHONUNBUFFERED + allowed_env_keys

### 2.2 `_check_command_filter(command)`

```python
def _check_command_filter(self, command: str) -> str | None
```
灵活命令过滤。
- allow 匹配 → None（允许）
- deny 匹配 → 错误消息
- allowlist 模式不匹配 → 错误消息
- 否则 → None（允许）

### 2.3 `_apply_path(env, command)`

```python
def _apply_path(self, env: dict[str, str], command: str) -> tuple[dict[str, str], str]
```
应用 PATH 修改。
- Windows：修改 env["PATH"]
- Unix：命令前加 export PATH 前缀

## 3. ExecTool 修改的方法

### 3.1 `__init__` 新增参数

| 参数 | 类型 | 默认值 |
|------|------|--------|
| `allowed_env_keys` | list[str] \| None | None |
| `allow_patterns` | list[str] \| None | None |
| `deny_patterns` | list[str] \| None | None |
| `path_prepend` | str | "" |
| `path_append` | str | "" |

### 3.2 `execute` 行为变化

- 使用 `_build_env()` 构建环境，传给 `create_subprocess_shell(env=...)`
- 危险检查改为 `_check_command_filter()`（包含默认黑名单 + 配置 deny + allow）
- 应用 PATH 修改
- 非零退出码时输出第一行加 `[exit code N]` 前缀

## 4. 输出格式变化

非零退出码：
```
[exit code {N}]
{stdout}
STDERR:
{stderr}

Exit code: {N}
```

零退出码：同 step69（无 [exit code] 前缀）。

## 5. 向后兼容

- 所有 step69 参数和行为保持不变
- 新增配置字段均有默认值，不配置时行为同 step69
- `deny_patterns` 默认包含 step69 的 `_DEFAULT_DENY_PATTERNS`
