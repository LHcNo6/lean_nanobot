# Step 110 API 契约：Memory 模块最终状态

## 概述

step110 无 API 变更。本文档记录 memory 主题对齐完成后（step92-step110）的最终 API 状态。

## memory.py — MemoryStore

### 类常量
- `_DEFAULT_MAX_HISTORY = 1000`
- `_DREAM_CONTENT_PATHS = ("SOUL.md", "USER.md", "memory/MEMORY.md")`
- `_DREAM_FILE_EMBED_CAP = 8000`
- `_INTERNAL_HISTORY_SESSION_PREFIXES = ("cron:", "dream:")`
- `_INTERNAL_HISTORY_SESSION_KEYS = {"heartbeat"}`
- `_LEGACY_ENTRY_START_RE` / `_LEGACY_TIMESTAMP_RE` / `_LEGACY_RAW_MESSAGE_RE`

### 公共方法

| 方法 | 签名 | 说明 |
|------|------|------|
| `__init__` | `(workspace, max_history_entries=1000)` | 初始化，含 GitStore 集成和 legacy 自动迁移 |
| `append_history` | `(entry, *, max_chars=None, session_key=None) -> int` | 追加历史，集成 strip_think，返回 cursor |
| `read_unprocessed_history` | `(since_cursor: int) -> list[dict]` | 返回 cursor > since_cursor 的有效条目 |
| `read_recent_history_for_prompt` | `(since_cursor, *, session_key, unified_session=False) -> list[dict]` | 按会话过滤的近期历史，用于 prompt 注入 |
| `raw_archive` | `(messages, *, max_chars=None, session_key=None) -> int` | 原始消息归档，带消息计数和 warning 日志 |
| `compact_history` | `() -> None` | 超过 max_history_entries 时丢弃最旧条目 |
| `read_memory` / `write_memory` | `() -> str` / `(content: str) -> None` | MEMORY.md 读写 |
| `read_soul` / `write_soul` | `() -> str` / `(content: str) -> None` | SOUL.md 读写 |
| `read_user` / `write_user` | `() -> str` / `(content: str) -> None` | USER.md 读写 |
| `get_memory_context` | `() -> str` | 返回长期记忆上下文（用于 system prompt） |
| `get_last_dream_cursor` / `set_last_dream_cursor` | `() -> int` / `(cursor: int) -> None` | Dream cursor 读写 |
| `get_latest_cursor` | `() -> int` | 返回最新 cursor（max(_next_cursor()-1, 0)） |
| `build_dream_prompt` | `(*, max_entries=20) -> tuple[str, int] \| None` | 构建 Dream prompt（模板化） |
| `dream_content_diff` | `() -> str` | Git 工作树差异摘要 |
| `build_dream_tools` | `() -> ToolRegistry` | Dream 受限工具集 |
| `has_dream_prompt_override` | `() -> bool` | 检测 workspace dream prompt 覆盖 |
| `dream_run_completed` | `(resp) -> bool` | 静态方法，判断 Dream run 是否正常完成 |
| `dream_session_key` | `() -> str` | 静态方法，生成 `dream:timestamp` 会话键 |
| `build_dream_commit_message` | `(prefix, diff_body) -> str` | 静态方法，基于 diff 构建 commit message |
| `prune_dream_sessions` | `(sessions_dir, *, keep=10) -> None` | 静态方法，清理旧 dream 会话 |

### 属性
- `git -> GitStore`：GitStore 实例
- `dream_prompt_file -> Path`：workspace dream prompt 文件路径

## consolidation.py — Consolidator

### 公共方法

| 方法 | 签名 | 说明 |
|------|------|------|
| `__init__` | `(store, sessions, build_messages, get_tool_definitions, consolidation_ratio=0.5, unified_session=False)` | 初始化，_locks 为 WeakValueDictionary |
| `get_lock` | `(session_key: str) -> asyncio.Lock` | 获取会话级压缩锁 |
| `pick_consolidation_boundary` | `(session, tokens_to_remove) -> tuple[int, int] \| None` | 选择用户轮次边界 |
| `estimate_session_prompt_tokens` | `(session, *, runtime) -> tuple[int, str]` | 完整 prompt token 估算（含 system+tools） |
| `archive` | `(messages, *, runtime, session_key=None, summary_messages=None) -> str \| None` | LLM 摘要归档，失败时 raw_archive 回退 |
| `maybe_consolidate_by_tokens` | `(session, *, runtime, replay_max_messages=None) -> None` | token 预算驱动的多轮压缩 |
| `compact_idle_session` | `(session_key, *, runtime, max_suffix=8) -> str` | 空闲会话硬截断压缩 |

## autocompact.py — AutoCompact

与参考实现对齐，支持空闲会话自动压缩和摘要注入。

## utils/gitstore.py — GitStore

| 方法 | 说明 |
|------|------|
| `is_initialized() -> bool` | Git 仓库是否初始化 |
| `init() -> None` | 初始化 git 仓库 |
| `summarize_working_tree(paths) -> str` | 工作树差异结构化摘要 |
| `auto_commit(message) -> str \| None` | 自动提交，返回 commit SHA |

## utils/workspace_prompts.py

| 函数/常量 | 说明 |
|-----------|------|
| `WORKSPACE_PROMPT_MAX_CHARS` | workspace prompt 最大字符数 |
| `workspace_prompt_file(workspace, name) -> Path` | 返回 workspace prompt 文件路径 |
| `has_workspace_prompt_override(path) -> bool` | 检测是否存在自定义覆盖 |
| `load_workspace_prompt_override(path) -> tuple[str \| None, int]` | 加载覆盖内容，超限截断 |

## helpers.py 新增

| 函数 | 说明 |
|------|------|
| `truncate_text_to_tokens(text, max_tokens) -> str` | 按 token 数截断文本 |
