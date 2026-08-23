# Step 83 API Specification

## 1. CliApp 数据类

**文件**：`tools/cli_apps.py`

```python
@dataclass
class CliApp:
    name: str           # 应用名称（唯一标识）
    command: str        # 入口命令（可执行文件路径）
    description: str = ""  # 应用描述
```

## 2. CliAppManager API

### register()

```python
def register(self, app: CliApp) -> None
```

注册一个 CLI 应用。同名应用会被覆盖。

### get()

```python
def get(self, name: str) -> CliApp | None
```

按名称获取应用，不存在返回 None。

### list_names()

```python
def list_names(self) -> list[str]
```

列出所有已注册应用名称。

### run()

```python
async def run(
    self,
    name: str,
    args: list[str] | None = None,
    cwd: str | None = None,
    timeout: int = 60,
) -> str
```

执行已注册的 CLI 应用。

| 参数 | 类型 | 说明 |
|------|------|------|
| `name` | string | 应用名称 |
| `args` | list[string] | 命令行参数列表 |
| `cwd` | string | 工作目录（可选） |
| `timeout` | int | 超时秒数（默认60） |

返回 stdout+stderr 文本。未知应用抛 ValueError。

## 3. CliAppsTool API

### 工具参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | string | 是 | 已注册的 CLI 应用名称 |
| `args` | list[string] | 否 | 命令行参数 |
| `working_dir` | string | 否 | 工作目录 |
| `timeout` | int | 否 | 超时秒数（1-600） |

### 工具元数据

- 名称：`run_cli_app`
- config_key：`cli_apps`
- 只读：否

### create()

从上下文创建，优先使用 `ctx.cli_app_manager`，否则创建空管理器。
