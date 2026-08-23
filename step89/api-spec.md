# Step 89 API Specification

## 1. sandbox.py API

**文件**：`tools/sandbox.py`

### wrap_command()

```python
def wrap_command(sandbox: str, command: str, workspace: str, cwd: str) -> str
```

根据沙箱后端名称包装命令。

| 参数 | 类型 | 说明 |
|------|------|------|
| `sandbox` | string | 沙箱后端名称（"none"/"bwrap"） |
| `command` | string | 原始 shell 命令 |
| `workspace` | string | workspace 路径 |
| `cwd` | string | 当前工作目录 |

返回包装后的命令字符串。

### 可用后端

| 名称 | 说明 | 平台 |
|------|------|------|
| `none` | 不包装（默认） | 全平台 |
| `bwrap` | bubblewrap 沙箱 | Linux |

## 2. ExecTool 集成

ExecTool 已有的 `sandbox` 配置字段（config.exec.sandbox）：
- 值为 `""` 或 `"none"`：不包装
- 值为 `"bwrap"`：使用 bwrap 沙箱包装

执行流程：在创建子进程前，若 sandbox 非空且非 "none"，调用 wrap_command 包装。

## 3. 后端注册机制

```python
_BACKENDS = {"none": _none, "bwrap": _bwrap}
```

新增后端只需实现函数并注册到 _BACKENDS。
