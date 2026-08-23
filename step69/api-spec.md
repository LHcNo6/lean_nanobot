# Step 69 API Specification

## 1. ExecTool API

**文件**：`tools/shell.py`
**继承**：`Tool`

| 属性 | 值 |
|------|-----|
| `name` | `"exec"` |
| `config_key` | `"exec"` |
| `_scopes` | `{"core", "subagent"}` |
| `_MAX_TIMEOUT` | `600`（秒） |
| `_MAX_OUTPUT` | `10_000`（字符） |

### 1.1 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `command` | string | 是 | — | 要执行的 shell 命令 |
| `working_dir` | string | 否 | workspace 根 | 命令执行的工作目录 |
| `timeout` | integer | 否 | `60` | 超时秒数（1-600，0=不限制） |

### 1.2 返回值

成功时返回文本，格式：
```
{stdout 内容}
STDERR:
{stderr 内容}

Exit code: {退出码}
```

无输出时返回 `"(no output)"`。

失败时返回 `ToolResult.error(...)`：
- 空 command：`"Error: Missing command."`
- 危险命令：`"Error: Command blocked for safety: matches '{pattern}'."`
- working_dir 越界：`"Error: working_dir is outside the configured workspace."`
- 超时：`"Error: Command timed out after {N} seconds"`
- 执行异常：`"Error executing command: {exc}"`

### 1.3 输出截断

当输出超过 `_MAX_OUTPUT`（10000 字符）时，头尾各保留一半，中间省略：
```
{前 5000 字符}

... ({N} chars truncated) ...

{后 5000 字符}
```

## 2. 危险命令黑名单

**常量**：`_DEFAULT_DENY_PATTERNS`（list[str]）

| 正则 | 匹配内容 |
|------|---------|
| `\brm\s+-[rf]{1,2}\b` | rm -r, rm -rf, rm -fr |
| `\bformat\b` | format |
| `\b(mkfs\|diskpart)\b` | mkfs, diskpart |
| `\bdd\s+if=` | dd if= |
| `>\s*/dev/sd` | 写磁盘设备 |
| `\b(shutdown\|reboot\|poweroff)\b` | shutdown, reboot, poweroff |
| `:\(\)\s*\{.*\};\s*:` | fork bomb |

检测方式：`re.search(pattern, command)`，匹配任意一个则拦截。

## 3. 配置契约

`config/schema.py` 中 `ExecToolConfig`：

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `enable` | bool | `True` | 是否启用 exec 工具 |
| `timeout` | int | `60` | 默认超时秒数（0=不限制） |
| `sandbox` | string | `""` | 沙箱配置（step69 未使用） |

`ToolsConfig.exec: ExecToolConfig`

## 4. 工具发现契约

`ToolLoader` 扫描 `tools/shell.py` 时：
- `ExecTool` 是具体 Tool 子类 → 被发现
- `_DEFAULT_DENY_PATTERNS` 以下划线开头 → 被过滤
- 辅助函数 → 被过滤

最终注册的工具名：`exec`

## 5. 方法签名

### 5.1 `ExecTool.execute`

```python
async def execute(
    self,
    command: str | None = None,
    working_dir: str | None = None,
    timeout: int | None = None,
    **kwargs: Any,
) -> str | ToolResult
```

### 5.2 `ExecTool._check_dangerous`

```python
def _check_dangerous(self, command: str) -> str | None
```
返回错误消息（被拦截时）或 None（安全时）。

### 5.3 `ExecTool._resolve_cwd`

```python
def _resolve_cwd(self, working_dir: str | None) -> str
```

### 5.4 `ExecTool._check_workspace_boundary`

```python
def _check_workspace_boundary(self, cwd: str) -> str | None
```
返回错误消息（越界时）或 None（合法时）。

### 5.5 `ExecTool._truncate_output`

```python
def _truncate_output(self, text: str, max_len: int) -> str
```
