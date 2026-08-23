# Step 65 API Specification

## 1. 配置层 API

### 1.1 `FileToolsConfig`

**文件**：`config/schema.py`

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `enable` | `bool` | `True` | 文件系统工具是否启用 |

继承自 `Base`（支持 camelCase / snake_case 双写）。

### 1.2 `ToolsConfig` 扩展

**文件**：`config/schema.py`

新增字段：

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `file` | `FileToolsConfig` | `FileToolsConfig()` | 文件系统工具配置 |

## 2. 上下文层 API

### 2.1 `ToolContext` 扩展

**文件**：`context.py`

新增字段：

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `file_state_store` | `Any` | `None` | `FileStateStore` 实例，按 session_key 管理 FileStates |

## 3. 工具层 API

### 3.1 `_FsTool`（抽象基类）

**文件**：`tools/filesystem.py`

**继承**：`Tool`

#### 类属性

| 属性 | 类型 | 值 | 说明 |
|------|------|-----|------|
| `config_key` | `str` | `"file"` | 配置节名，对应 `ToolsConfig.file` |
| `_scopes` | `set[str]` | `{"core"}` | 工具适用范围（子类可覆盖） |

#### 类方法

| 方法 | 签名 | 说明 |
|------|------|------|
| `config_cls()` | `→ type[BaseModel]` | 返回 `FileToolsConfig` |
| `enabled(ctx)` | `(ctx: ToolContext) → bool` | 读取 `ctx.config.tools.file.enable` |
| `create(ctx)` | `(ctx: ToolContext) → Tool` | 从 ToolContext 创建实例 |

#### 实例属性（protected）

| 属性 | 类型 | 说明 |
|------|------|------|
| `_workspace` | `str` | 项目根目录 |
| `_restrict` | `bool` | 是否限制在 workspace 内 |
| `_explicit_file_states` | `FileStates \| None` | 显式传入的 FileStates（隔离场景） |
| `_fallback_file_states` | `FileStates` | 无绑定时的回退实例 |
| `_allowed_dir` | `str \| None` | 显式允许根 |

#### 实例属性（property）

| 属性 | 类型 | 说明 |
|------|------|------|
| `_file_states` | `FileStates` | 优先显式 → ContextVar → fallback |

#### 实例方法（protected）

| 方法 | 签名 | 说明 |
|------|------|------|
| `_resolve_write(path)` | `(path: str) → Path` | 解析写入路径，应用边界守卫 |

### 3.2 `WriteFileTool`

**文件**：`tools/filesystem.py`

**继承**：`_FsTool`

#### 类属性

| 属性 | 值 | 说明 |
|------|-----|------|
| `_scopes` | `{"core", "subagent", "memory"}` | 适用范围 |

#### 实例属性（property）

| 属性 | 类型 | 值 | 说明 |
|------|------|-----|------|
| `name` | `str` | `"write_file"` | 工具名 |
| `description` | `str` | （见下） | 工具描述 |
| `read_only` | `bool` | `False` | 有副作用 |

`description` 全文：
> Create a new file or intentionally replace an entire file with the provided content.
> Overwrites existing files and creates parent directories as needed.

#### 参数 Schema

通过 `@tool_parameters` 装饰器定义：

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `path` | `string` | 是 | 写入目标文件路径（绝对或 workspace 相对） |
| `content` | `string` | 是 | 要写入的文本内容 |

#### 方法

| 方法 | 签名 | 说明 |
|------|------|------|
| `execute(path, content, **kwargs)` | `async (path: str, content: str, **kwargs) → ToolResult` | 执行写入 |

**返回值**：
- 成功：`ToolResult("Successfully wrote {N} characters to {path}")`
- 失败：`ToolResult.error("Error: ...")`

**异常处理**：
| 异常 | 返回消息 |
|------|---------|
| 空 path | `Error: write_file requires a 'path' parameter.` |
| None content | `Error: write_file requires a 'content' parameter.` |
| `WorkspaceBoundaryError` | `Error: {exc}` |
| `PermissionError` | `Error: {exc}` |
| `OSError` | `Error writing file: {exc}` |

#### 副作用

1. 创建/覆盖文件（UTF-8 编码）
2. 自动创建父目录（`mkdir(parents=True, exist_ok=True)`）
3. 调用 `self._file_states.record_write(fp)` 更新文件状态

## 4. Loop 层 API

### 4.1 `AgentLoop.__init__` 扩展

**文件**：`loop.py`

新增实例属性：

| 属性 | 类型 | 说明 |
|------|------|------|
| `_file_state_store` | `FileStateStore` | 跨会话共享的文件状态查找表 |

### 4.2 `_process_message` 中 ToolContext 创建

**文件**：`loop.py`

`ToolContext(...)` 调用新增参数：`file_state_store=self._file_state_store`

## 5. 工具发现契约

`ToolLoader` 扫描 `tools/` 包时：
- `filesystem.py` 不在 `_SKIP_MODULES` 中 → 被扫描
- `WriteFileTool` 是 `Tool` 的具体子类（无抽象方法）→ 被发现
- `_FsTool` 以下划线开头 → 被 `not attr_name.startswith("_")` 过滤
- `FileToolsConfig` 不是 `Tool` 子类 → 被过滤

最终注册的工具名：`write_file`
