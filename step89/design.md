# Step 89 Design: ExecTool 沙箱后端抽象

## 1. 架构

```
tools/sandbox.py（新建）
  ├── _none(command, workspace, cwd)     默认无沙箱
  ├── _bwrap(command, workspace, cwd)    bwrap 沙箱
  ├── _BACKENDS                          后端注册字典
  └── wrap_command(sandbox, command, workspace, cwd)  入口函数

tools/shell.py（修改）
  └── ExecTool._execute / _execute_session  集成 wrap_command
```

## 2. 沙箱后端函数签名

```python
def _backend(command: str, workspace: str, cwd: str) -> str
```

接收原始命令、workspace 路径、当前工作目录，返回包装后的命令字符串。

## 3. bwrap 后端（对齐 nanobot）

- 只读绑定系统目录（/usr, /bin, /lib 等）
- /proc, /dev, /tmp 独立
- workspace 读写绑定
- media 目录只读绑定
- 隐藏 workspace 父目录（防止访问 config.json）
- chdir 到 sandbox 内的 cwd

## 4. ExecTool 集成

在 ExecTool 执行命令前：
```python
if self.sandbox:
    command = wrap_command(self.sandbox, command, workspace, cwd)
```

config.exec.sandbox 字段已存在（step69），值为 "" 表示无沙箱。

## 5. 测试策略

- wrap_command none 后端
- wrap_command bwrap 后端（验证 bwrap 参数）
- 未知沙箱报错
- ExecTool 集成（mock wrap_command 验证调用）
- sandbox="" 时不调用 wrap_command
