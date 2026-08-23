# Step 83 Design: CliAppsTool

## 1. 架构

```
tools/cli_apps.py（新建）
  ├── CliApp                    数据类（name/command/description）
  ├── CliAppManager             应用管理器
  │   ├── register(app)         注册应用
  │   ├── get(name)             获取应用
  │   ├── list_names()          列出应用名
  │   └── run(name, args, ...)  执行应用
  └── CliAppsTool               工具类
      ├── create(ctx)           从上下文创建
      └── execute(name, args)   执行应用
```

## 2. CliAppManager

```python
class CliAppManager:
    def __init__(self):
        self._apps: dict[str, CliApp] = {}

    def register(self, app: CliApp) -> None
    def get(self, name: str) -> CliApp | None
    def list_names(self) -> list[str]
    async def run(self, name, args, cwd, timeout) -> str
```

run 方法：
1. 查找应用，不存在则抛 ValueError
2. 构建 argv = [app.command] + args
3. 使用 asyncio.create_subprocess_exec 执行
4. 等待完成，返回 stdout/stderr

## 3. CliAppsTool

```python
@tool_parameters(
    name=StringSchema("CLI app name"),
    args=ArraySchema(StringSchema("argument"), nullable=True),
    working_dir=StringSchema(nullable=True),
    timeout=IntegerSchema(minimum=1, maximum=600, nullable=True),
)
class CliAppsTool(Tool):
    name = "run_cli_app"
    config_key = "cli_apps"
```

create 从 ctx 获取 cli_app_manager（如果有），否则创建空的。

## 4. 与 ExecTool 的区别

- ExecTool：shell 执行（create_subprocess_shell），任意命令
- CliAppsTool：argv 执行（create_subprocess_exec），仅限已注册应用

## 5. 测试策略

- CliAppManager 注册/查询/列出
- CliAppsTool 执行已注册应用
- 未知应用名报错
- 参数传递正确
- 超时处理
