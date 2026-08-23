# Step 65 Proposal: WriteFileTool + ToolContext 扩展

## 1. 问题背景

learn_nano step64 的工具系统仅有 `read_file` 一个文件操作工具，缺少写入能力。
agent 无法创建新文件或覆盖已有文件，导致"读-改-写"闭环断裂。

nanobot 的文件系统工具集中在 `agent/tools/filesystem.py`，包含 `_FsTool` 基类
以及 `ReadFileTool`、`WriteFileTool`、`EditFileTool`、`ListDirTool` 四个工具，
共享路径解析、workspace 边界守卫和文件状态追踪。

## 2. 目标

以最小增量引入 `WriteFileTool`，使 agent 具备文件写入能力：

1. `config/schema.py`：新增 `FileToolsConfig`，`ToolsConfig` 添加 `file` 字段
2. `context.py`：`ToolContext` 添加 `file_state_store` 字段
3. `tools/filesystem.py`：新建文件，含 `_FsTool` 基类 + `WriteFileTool`
4. `loop.py`：创建 `FileStateStore` 实例并传入 `ToolContext`

## 3. 非目标（明确不做）

- **不迁移** 现有的 `tools/read_file.py` 到 `filesystem.py`（保持 step65 增量最小，
  ReadFileTool 的迁移留给后续 step 统一处理）
- **不实现** `EditFileTool`、`ListDirTool`（step66、step67）
- **不实现** PDF/Office 文档读取、图片内容块等高级特性
- **不引入** `path_utils.py`（直接复用现有 `resolve_allowed_path`）
- **不实现** nanobot 的 `extra_write_allowed_dirs`/`extra_write_allowed_files`
  细粒度白名单（当前 `resolve_allowed_path` 已支持 `extra_allowed_roots`，
  足够覆盖基础场景）

## 4. 方案选择

### 方案 A：独立 WriteFileTool（不建基类）
直接在 `tools/write_file.py` 中写一个独立工具，复制路径解析逻辑。
- 优点：改动最小
- 缺点：与后续 EditFileTool/ListDirTool 会产生大量重复代码，不符合 DRY

### 方案 B：新建 `_FsTool` 基类 + `WriteFileTool`（选定）
在 `tools/filesystem.py` 中建立 `_FsTool` 基类，封装共享的路径解析和
文件状态追踪逻辑，`WriteFileTool` 继承它。
- 优点：为 step66/step67 铺路，对齐 nanobot 架构
- 缺点：比方案 A 多一个基类，但基类本身是必要的基础设施

**选择方案 B**。`_FsTool` 是后续三个文件工具的共享依赖，提前建立避免重复劳动。

## 5. 关键设计决策

### 5.1 `_FsTool` 简化策略
nanobot 的 `_FsTool.__init__` 有 9 个参数（含多种 extra_allowed_* 白名单）。
learn_nano 简化为 4 个核心参数：
- `workspace`：项目根
- `restrict_to_workspace`：是否受限
- `file_states`：显式 FileStates（用于 dream/subagent 等隔离场景）
- `allowed_dir`：显式允许根（受限模式下使用）

路径解析直接调用现有 `resolve_allowed_path`，不引入 `path_utils.py`。

### 5.2 `file_state_store` 的生命周期
- `AgentLoop.__init__` 中创建一个 `FileStateStore` 实例（跨会话共享的查找表）
- 每次 `_process_message` 构建 `ToolContext` 时传入该 store
- 工具通过 `ctx.file_state_store` 获取，再按 `session_key` 取具体 `FileStates`

### 5.3 与现有 `read_file.py` 的共存
step65 不删除 `tools/read_file.py`。两个 ReadFileTool 会同时存在：
- `tools/read_file.py`：旧版，继续被 ToolLoader 发现
- `tools/filesystem.py` 中的 ReadFileTool：step65 **不创建**

ToolLoader 的 `_SKIP_MODULES` 不需要修改，因为 `filesystem.py` 不在跳过列表中，
且其中只有 `WriteFileTool` 一个具体工具类。

## 6. 验收标准

1. `WriteFileTool` 可被 `ToolLoader` 自动发现并注册
2. 写入新文件成功，返回包含字符数和路径的消息
3. 覆盖已有文件成功，父目录自动创建
4. 受限模式下写入 workspace 外路径返回 `ToolResult.error`
5. 写入后 `FileStates.record_write` 被调用（标记不可 dedup）
6. 所有现有测试通过，新增 `WriteFileTool` 单元测试
7. `python -m pytest tests/test_filesystem.py` 通过
