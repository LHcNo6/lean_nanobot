# step65：WriteFileTool + ToolContext 扩展

## 1. 问题背景

learn_nano step64 的工具系统仅有 `read_file` 一个文件操作工具，缺少写入能力。
agent 无法创建新文件或覆盖已有文件，导致"读-改-写"闭环断裂——agent 能读文件、
能分析内容，但无法把修改结果写回磁盘。

nanobot 的文件系统工具集中在 `agent/tools/filesystem.py`，包含 `_FsTool` 基类
以及 `ReadFileTool`、`WriteFileTool`、`EditFileTool`、`ListDirTool` 四个工具，
共享路径解析、workspace 边界守卫和文件状态追踪。

step65 以最小增量引入 `WriteFileTool`，使 agent 具备文件写入能力，同时建立
`_FsTool` 基类为后续 step66（EditFileTool）、step67（ListDirTool）铺路。

## 2. 原理分析

### 2.1 为什么需要 `_FsTool` 基类？

文件系统工具（read/write/edit/list）共享三组逻辑：
1. **路径解析**：把用户传入的相对/绝对路径解析为绝对 Path，并应用 workspace
   边界守卫（`resolve_allowed_path`）；
2. **文件状态追踪**：读写后更新 `FileStates`，支持 read-dedup（避免重复读取
   未变化的文件）和 read-before-edit（编辑前警告未读取的文件）；
3. **配置与创建**：从 `ToolContext` 创建实例，读取 `config.tools.file.enable`
   判断是否启用。

如果每个工具各自实现这些逻辑，会产生大量重复代码，且边界守卫行为可能不一致。
`_FsTool` 基类封装共享逻辑，子类只需实现 `name`/`description`/`execute`。

### 2.2 为什么 `FileStates` 要按 session_key 隔离？

`FileStates` 记录文件的读取/写入状态，用于 read-dedup 和 read-before-edit。
如果跨会话共享状态，会出现：
- 会话 A 读取了文件 X，会话 B 从未读取但也能 dedup（错误）；
- 会话 A 写入了文件 X，会话 B 的 read-before-edit 检查被绕过（安全风险）。

因此通过 `FileStateStore.for_session(session_key)` 按会话隔离，每个会话有独立
的 `FileStates` 实例。

### 2.3 为什么写操作不享受读豁免目录？

nanobot 的 `_FsTool` 区分 `_resolve_read` 和 `_resolve_write`：
- 读操作允许访问内置技能目录（`BUILTIN_SKILLS_DIR`），因为 agent 需要读取
  SKILL.md 来了解技能用法；
- 写操作不允许访问豁免目录，防止 agent 意外修改内置技能文件。

step65 的 `_resolve_write` 不传入 `extra_allowed_roots`，与 nanobot 语义一致。

### 2.4 为什么不迁移现有 `read_file.py`？

step65 的目标是最小增量引入写入能力。现有 `tools/read_file.py` 功能完整且测试
覆盖充分，迁移到 `filesystem.py` 会增加改动范围和回归风险。ReadFileTool 的迁移
留给后续 step 统一处理（届时可同时升级到带行号分页的版本）。

## 3. 实现方案

### 3.1 配置层扩展（`config/schema.py`）

新增 `FileToolsConfig`：
```python
class FileToolsConfig(Base):
    enable: bool = True  # 内置文件工具默认开启
```

`ToolsConfig` 添加 `file` 字段：
```python
class ToolsConfig(Base):
    # ...
    file: FileToolsConfig = Field(default_factory=FileToolsConfig)
```

### 3.2 ToolContext 扩展（`context.py`）

`ToolContext` 添加 `file_state_store` 字段：
```python
@dataclass
class ToolContext:
    # ...
    file_state_store: Any = None  # FileStateStore 实例
```

### 3.3 文件系统工具（`tools/filesystem.py`，新建）

#### `_FsTool` 基类

核心方法：
- `create(ctx)`：从 ToolContext 创建实例，按 session_key 获取 FileStates；
- `_file_states`（property）：优先显式 > ContextVar > fallback；
- `_resolve_write(path)`：解析写入路径，应用边界守卫（无豁免目录）；
- `enabled(ctx)`：读取 `config.tools.file.enable`。

#### `WriteFileTool`

```python
@tool_parameters(tool_parameters_schema(
    path=StringSchema("The file path to write to"),
    content=StringSchema("The content to write"),
    required=["path", "content"],
))
class WriteFileTool(_FsTool):
    async def execute(self, path="", content="", **kwargs):
        fp = self._resolve_write(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")
        self._file_states.record_write(fp)
        return ToolResult(f"Successfully wrote {len(content)} characters to {fp}")
```

### 3.4 Loop 集成（`loop.py`）

- `__init__` 中创建 `self._file_state_store = FileStateStore()`；
- `_process_message` 中 `ToolContext(...)` 传入 `file_state_store=self._file_state_store`。

## 4. 核心类/函数说明

### `_FsTool`（抽象基类）

文件系统工具共享基类，封装路径解析、文件状态追踪、配置读取。子类必须实现
`name`/`description`/`execute`。

关键属性：
- `_workspace`：项目根目录；
- `_restrict`：是否限制在 workspace 内；
- `_explicit_file_states`：显式传入的 FileStates（隔离场景）；
- `_fallback_file_states`：无绑定时的回退实例。

关键方法：
- `create(ctx)`：类方法，从 ToolContext 创建实例；
- `_resolve_write(path)`：解析写入路径（无豁免目录）；
- `_file_states`（property）：获取当前生效的 FileStates。

### `WriteFileTool`

写入文件工具，继承 `_FsTool`。

功能：
- 创建新文件或覆盖已有文件；
- 自动创建不存在的父目录；
- UTF-8 编码写入；
- 写入后更新 FileStates（标记不可 dedup）。

参数：
- `path`（必填）：目标文件路径；
- `content`（必填）：要写入的文本内容。

返回：
- 成功：`"Successfully wrote {N} characters to {path}"`；
- 失败：`ToolResult.error("Error: ...")`。

### `FileStateStore`（已有，step65 开始被 loop 使用）

按 session_key 存储 FileStates 的查找表。`for_session(key)` 获取/创建对应会话的
FileStates 实例。

## 5. 文件修改清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `config/schema.py` | 修改 | 新增 `FileToolsConfig`，`ToolsConfig` + `file` 字段 |
| `context.py` | 修改 | `ToolContext` + `file_state_store` 字段 |
| `tools/filesystem.py` | 新建 | `_FsTool` 基类 + `WriteFileTool` |
| `loop.py` | 修改 | `__init__` 创建 `FileStateStore`，`ToolContext` 传入 |
| `tests/test_filesystem.py` | 新建 | 19 个单元测试 |
| `proposal.md` | 新建 | 需求定义 |
| `design.md` | 新建 | 架构设计 |
| `api-spec.md` | 新建 | 接口契约 |

## 6. 测试结果

- `tests/test_filesystem.py`：19 passed（写入/覆盖/父目录/状态/边界/错误/发现）
- `tests/test_file_state.py`：21 passed（无回归）
- `tests/test_workspace_tool.py`：18 passed（无回归）

## 7. 暴露问题与下一步

### 7.1 暴露的技术债

1. **ReadFileTool 仍在独立文件**：`tools/read_file.py` 与 `tools/filesystem.py`
   中的 `_FsTool` 基类并存，路径解析逻辑有重复。后续 step 应将 ReadFileTool
   迁移到 `filesystem.py` 并统一使用 `_FsTool`。

2. **`_FsTool` 简化版缺少细粒度白名单**：nanobot 的 `_FsTool` 支持
   `extra_write_allowed_dirs`/`extra_write_allowed_files` 等细粒度白名单，
   step65 简化版只支持 workspace 根级限制。后续如需要可扩展。

3. **无设备文件黑名单**：nanobot 的 ReadFileTool 有 `/dev/*` 设备文件黑名单
   （防止读取 `/dev/zero` 等导致挂起）。step65 的 WriteFileTool 未实现此保护，
   Windows 下不适用，但 Linux 部署时需要补充。

4. **ToolContext 仍缺少多个 nanobot 字段**：`cron_service`/`exec_session_manager`/
   `provider_snapshot_loader`/`image_generation_provider_configs`/`timezone`/
   `workspace_sandbox`/`runtime_events` 等字段尚未添加，将在后续工具迁移时按需补充。

### 7.2 下一步（step66）

**EditFileTool**：在 `tools/filesystem.py` 中新增 `EditFileTool`，实现精确字符串
替换（`old_string` → `new_string`），支持 `replace_all`，集成 `FileStates.check_read`
实现 read-before-edit 警告。依赖 step65 的 `_FsTool` 基类和 `file_state.py`。
