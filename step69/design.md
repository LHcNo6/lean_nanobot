# Step 69 Design: ExecTool 基础版

## 1. 架构概览

```
tools/shell.py（新建）
  ├── ExecToolConfig          配置（已有，在 config/schema.py）
  ├── _DEFAULT_DENY_PATTERNS  危险命令正则列表
  └── ExecTool(Tool)           shell 命令执行工具
```

## 2. 模块详细设计

### 2.1 ExecTool 类

#### 类属性

```python
class ExecTool(Tool):
    _scopes = {"core", "subagent"}
    config_key = "exec"
    _MAX_TIMEOUT = 600       # 单次最大超时（秒）
    _MAX_OUTPUT = 10_000     # 默认最大输出字符数
```

#### 构造函数

```python
def __init__(
    self,
    timeout: int = 60,
    working_dir: str | None = None,
    restrict_to_workspace: bool = False,
    deny_patterns: list[str] | None = None,
):
    self.timeout = timeout
    self.working_dir = working_dir
    self.restrict_to_workspace = restrict_to_workspace
    self.deny_patterns = deny_patterns or list(_DEFAULT_DENY_PATTERNS)
```

#### create 类方法

```python
@classmethod
def create(cls, ctx: Any) -> Tool:
    cfg = ctx.config.exec
    return cls(
        working_dir=ctx.workspace,
        timeout=cfg.timeout,
        restrict_to_workspace=ctx.config.tools.restrict_to_workspace,
    )
```

#### enabled 类方法

```python
@classmethod
def enabled(cls, ctx: Any) -> bool:
    return ctx.config.exec.enable
```

#### 参数 Schema

```python
@tool_parameters(tool_parameters_schema(
    command=StringSchema("The shell command to execute"),
    working_dir=StringSchema("Optional working directory for the command"),
    timeout=IntegerSchema(
        "Timeout in seconds (default 60, max 600)",
        minimum=1, maximum=600,
    ),
    required=["command"],
))
```

#### execute 流程

```
1. 参数校验
   └─ command 为空 → error

2. 危险命令检查
   └─ 匹配 deny_patterns → error("Error: Command blocked for safety: ...")

3. 解析 working_dir
   ├─ 优先用调用参数 working_dir
   ├─ 否则用 self.working_dir
   └─ 否则用 os.getcwd()

4. workspace 边界检查（restrict_to_workspace=True 时）
   └─ working_dir 不在 workspace 内 → error

5. 解析超时
   ├─ 优先用调用参数 timeout（不超过 _MAX_TIMEOUT）
   ├─ 否则用 self.timeout
   └─ 0 表示不限制

6. 创建子进程
   └─ asyncio.create_subprocess_shell(command, cwd=cwd, stdout=PIPE, stderr=PIPE)

7. 等待完成（带超时）
   ├─ 超时 → kill 进程，返回超时错误
   └─ 正常 → 获取 stdout/stderr

8. 组装输出
   ├─ stdout 内容
   ├─ stderr 内容（前缀 STDERR:）
   └─ 退出码（Exit code: N）

9. 输出截断
   └─ 超过 _MAX_OUTPUT 时头尾保留

10. 返回结果
```

#### 关键方法

| 方法 | 签名 | 说明 |
|------|------|------|
| `execute(command, working_dir, timeout, **kwargs)` | async | 主执行方法 |
| `_check_dangerous(command)` | `(str) -> str \| None` | 危险命令检查，返回错误消息或 None |
| `_resolve_cwd(working_dir)` | `(str \| None) -> str` | 解析工作目录 |
| `_check_workspace_boundary(cwd)` | `(str) -> str \| None` | workspace 边界检查 |
| `_truncate_output(text, max_len)` | `(str, int) -> str` | 输出截断 |

### 2.2 危险命令黑名单

```python
_DEFAULT_DENY_PATTERNS = [
    r"\brm\s+-[rf]{1,2}\b",          # rm -r, rm -rf, rm -fr
    r"\bformat\b",                      # format
    r"\b(mkfs|diskpart)\b",            # 磁盘操作
    r"\bdd\s+if=",                      # dd
    r">\s*/dev/sd",                     # 写磁盘
    r"\b(shutdown|reboot|poweroff)\b", # 系统电源
    r":\(\)\s*\{.*\};\s*:",             # fork bomb
]
```

使用 `re.search(pattern, command)` 检测，匹配到任意一个则拦截。

### 2.3 输出格式

```
{stdout content}
STDERR:
{stderr content}

Exit code: {code}
```

无输出时返回 `"(no output)"`。

### 2.4 输出截断

```python
def _truncate_output(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    half = max_len // 2
    truncated = len(text) - max_len
    return text[:half] + f"\n\n... ({truncated:,} chars truncated) ...\n\n" + text[-half:]
```

## 3. 配置集成

`config/schema.py` 中已有 `ExecToolConfig`：
```python
class ExecToolConfig(Base):
    enable: bool = True
    timeout: int = Field(default=60, ge=0)
    sandbox: str = ""
```

`ToolsConfig` 中已有 `exec: ExecToolConfig` 字段。

`ExecTool.enabled(ctx)` 读取 `ctx.config.exec.enable`。
`ExecTool.create(ctx)` 读取 `ctx.config.exec.timeout` 和 `ctx.config.tools.restrict_to_workspace`。

## 4. 错误处理

| 场景 | 返回消息 |
|------|---------|
| 空 command | `Error: Missing command.` |
| 危险命令 | `Error: Command blocked for safety: matches '{pattern}'.` |
| working_dir 越界 | `Error: working_dir is outside the configured workspace.` |
| 超时 | `Error: Command timed out after {N} seconds` |
| 子进程创建失败 | `Error executing command: {exc}` |

## 5. 安全边界

- **危险命令黑名单**：防止 rm -rf、format、mkfs 等破坏性命令
- **workspace 边界**：受限模式下 working_dir 不能越界
- **超时保护**：默认 60s 超时，防止命令挂起
- **输出截断**：默认 10000 字符，防止输出爆炸
- **非交互执行**：不支持交互式命令（需要用户输入的命令会挂起直到超时）

## 6. 测试策略

### `tests/test_exec.py`
1. `test_echo_command`：简单 echo 命令执行成功
2. `test_nonzero_exit_code`：非零退出码返回错误输出和退出码
3. `test_timeout`：超时命令被杀死并返回超时错误
4. `test_dangerous_rm_rf`：rm -rf 被拦截
5. `test_dangerous_format`：format 被拦截
6. `test_dangerous_shutdown`：shutdown 被拦截
7. `test_working_dir`：指定 working_dir 执行
8. `test_workspace_boundary`：受限模式下越界 working_dir 被拒绝
9. `test_output_truncation`：长输出被截断
10. `test_stderr_output`：stderr 输出正确显示
11. `test_no_output`：无输出返回提示
12. `test_custom_timeout`：自定义超时生效
13. `test_tool_discovered`：ToolLoader 自动发现
14. `test_tool_schema`：参数 schema 正确
15. `test_config_disabled`：config.exec.enable=False 时不加载

注意：测试使用跨平台命令（echo、python -c），避免 Unix/Windows 差异。
