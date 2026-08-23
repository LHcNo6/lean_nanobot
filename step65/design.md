# Step 65 Design: WriteFileTool + ToolContext 扩展

## 1. 架构概览

```
loop.py
  ├── self._file_state_store = FileStateStore()  [新增]
  └── _process_message()
        └── tool_ctx = ToolContext(..., file_state_store=self._file_state_store)  [修改]
              └── ToolLoader().load(tool_ctx, registry)
                    └── tools/filesystem.py  [新增]
                          ├── _FsTool(Tool)       ← 基类
                          └── WriteFileTool(_FsTool)
```

## 2. 模块详细设计

### 2.1 `config/schema.py` — 新增 FileToolsConfig

```python
class FileToolsConfig(Base):
    """文件系统工具配置（对齐 nanobot filesystem.FileToolsConfig）。"""
    enable: bool = True  # 内置文件工具默认开启

class ToolsConfig(Base):
    # ... 现有字段 ...
    file: FileToolsConfig = Field(default_factory=FileToolsConfig)  # 新增
```

**理由**：`_FsTool.enabled(ctx)` 需要读取 `ctx.config.tools.file.enable` 来判断
是否启用文件工具。与 nanobot 的 `config_key = "file"` + `config_cls()` 机制对齐。

### 2.2 `context.py` — ToolContext 扩展

```python
@dataclass
class ToolContext:
    # ... 现有字段 ...
    file_state_store: Any = None  # 新增：FileStateStore 实例
```

**理由**：`_FsTool.create(ctx)` 需要从 ctx 获取 `file_state_store`，用于按
session_key 查找对应的 `FileStates` 实例。

### 2.3 `tools/filesystem.py` — 新建

#### 2.3.1 `_FsTool` 基类

```python
class _FsTool(Tool):
    """文件系统工具共享基类：路径解析 + 文件状态追踪。"""
    config_key = "file"

    @classmethod
    def config_cls(cls):
        return FileToolsConfig

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        return ctx.config.tools.file.enable

    def __init__(self, workspace: str = "", restrict_to_workspace: bool = False,
                 file_states: FileStates | None = None, allowed_dir: str | None = None):
        self._workspace = workspace
        self._restrict = restrict_to_workspace
        self._explicit_file_states = file_states
        self._fallback_file_states = FileStates()
        self._allowed_dir = allowed_dir

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        from step65.skills.loader import BUILTIN_SKILLS_DIR
        restrict = ctx.config.tools.restrict_to_workspace
        allowed_dir = ctx.workspace if restrict else None
        file_states = None
        if ctx.file_state_store is not None:
            file_states = ctx.file_state_store.for_session(ctx.session_key)
        return cls(
            workspace=ctx.workspace,
            restrict_to_workspace=restrict,
            file_states=file_states,
            allowed_dir=allowed_dir,
        )

    @property
    def _file_states(self) -> FileStates:
        if self._explicit_file_states is not None:
            return self._explicit_file_states
        return current_file_states(self._fallback_file_states)

    def _resolve_write(self, path: str) -> Path:
        """解析写入路径，应用 workspace 边界守卫。"""
        access = current_tool_workspace(self._workspace, restrict_to_workspace=self._restrict)
        return resolve_allowed_path(
            path,
            workspace=access.project_path or (self._workspace or None),
            allowed_root=access.allowed_root,
        )
```

**关键设计点**：
- `_file_states` 属性优先使用显式传入的 `FileStates`，否则回退到 ContextVar
  绑定的当前 task 状态，最后回退到内置 fallback 实例。这与 nanobot 语义一致。
- `_resolve_write` 不传入 `extra_allowed_roots`（写操作不允许读技能目录等豁免路径）。
- `create(ctx)` 中通过 `ctx.file_state_store.for_session(ctx.session_key)` 获取
  会话级 FileStates，使 read-dedup 和 read-before-edit 限定在单会话内。

#### 2.3.2 `WriteFileTool`

```python
@tool_parameters(tool_parameters_schema(
    path=StringSchema("The file path to write to"),
    content=StringSchema("The content to write"),
    required=["path", "content"],
))
class WriteFileTool(_FsTool):
    """写入文件内容（创建新文件或覆盖已有文件）。"""
    _scopes = {"core", "subagent", "memory"}

    @property
    def name(self) -> str:
        return "write_file"

    @property
    def description(self) -> str:
        return ("Create a new file or intentionally replace an entire file with "
                "the provided content. Overwrites existing files and creates parent "
                "directories as needed.")

    async def execute(self, path: str = "", content: str = "", **kwargs: Any) -> ToolResult:
        if not path:
            return ToolResult.error("Error: write_file requires a 'path' parameter.")
        if content is None:
            return ToolResult.error("Error: write_file requires a 'content' parameter.")
        try:
            fp = self._resolve_write(path)
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(content, encoding="utf-8")
            self._file_states.record_write(fp)
            return ToolResult(f"Successfully wrote {len(content)} characters to {fp}")
        except WorkspaceBoundaryError as exc:
            return ToolResult.error(f"Error: {exc}")
        except PermissionError as exc:
            return ToolResult.error(f"Error: {exc}")
        except OSError as exc:
            return ToolResult.error(f"Error writing file: {exc}")
```

**关键设计点**：
- `fp.parent.mkdir(parents=True, exist_ok=True)`：自动创建父目录（对齐 nanobot）
- `self._file_states.record_write(fp)`：写入后标记文件状态为"已写入"，
  使后续 read dedup 失效（内容可能已变），并使 edit_file 的 read-before-edit
  检查知道文件刚被写过。
- 异常分类：`WorkspaceBoundaryError`（越界）、`PermissionError`（权限）、
  `OSError`（其他 IO 错误），分别返回清晰的错误消息。

### 2.4 `loop.py` — FileStateStore 集成

#### `__init__` 中新增
```python
from step65.tools.file_state import FileStateStore
self._file_state_store = FileStateStore()
```

#### `_process_message` 中 ToolContext 创建修改
```python
tool_ctx = ToolContext(
    config=self.config,
    workspace=str(scope.project_path),
    restrict_to_workspace=scope.restrict_to_workspace,
    bus=self.bus, subagent_manager=self.subagents,
    sessions=self.sessions, session_key=session_key,
    file_state_store=self._file_state_store,  # 新增
)
```

## 3. 数据流向

```
用户消息 → _process_message
  → scope = WorkspaceScopeResolver.resolve(...)
  → file_states = self._file_state_store.for_session(session_key)
  → tool_ctx = ToolContext(..., file_state_store=self._file_state_store)
  → ToolLoader.load(tool_ctx, registry)
      → WriteFileTool.create(tool_ctx)
          → self._explicit_file_states = file_state_store.for_session(session_key)
  → agent 调用 write_file(path="...", content="...")
      → _resolve_write(path) → Path（应用边界守卫）
      → fp.write_text(content)
      → self._file_states.record_write(fp)
      → 返回 ToolResult
```

## 4. 安全边界

- **路径越界**：`resolve_allowed_path` 在 `restrict_to_workspace=True` 时
  强制路径在 workspace 内，越界抛 `WorkspaceBoundaryError`
- **设备文件保护**：step65 暂不实现 `/dev/*` 黑名单（nanobot 的 `_is_blocked_device`
  是 Linux 特有的，Windows 下不适用，留待后续跨平台统一处理）
- **编码**：固定 UTF-8 写入，与 read_file 的读取编码一致

## 5. 测试策略

### 单元测试 `tests/test_filesystem.py`
1. `test_write_file_creates_new_file`：写入新文件，验证内容和返回消息
2. `test_write_file_overwrites_existing`：覆盖已有文件
3. `test_write_file_creates_parent_dirs`：父目录不存在时自动创建
4. `test_write_file_records_state`：写入后 `FileStates` 标记为不可 dedup
5. `test_write_file_restricted_boundary`：受限模式下写入越界路径返回错误
6. `test_write_file_empty_path`：空路径参数返回错误
7. `test_write_file_tool_discovered_by_loader`：ToolLoader 能自动发现

### 集成验证
- 运行现有测试套件确保无回归
- 手动运行 `python main.py` 验证 write_file 出现在工具列表中
