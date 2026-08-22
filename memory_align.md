# Memory 模块对齐路线图

> 基准：`nananobot/nanobot/agent/memory.py`（MemoryStore + Consolidator）
> 当前：`learn_nano/step91/memory.py`（MemoryStore ~45%）+ `consolidation.py`（Consolidator ~75%）
> 目标：step92 - step110，MemoryStore 对齐度 ≥98%，Consolidator 对齐度 ≥97%

---

## 当前对齐度快照（step91）

### MemoryStore（~45%）

| 类别 | 已对齐 | 未对齐 |
|------|--------|--------|
| 初始化 | 基本字段、cursor、lock | legacy_history_file、rate-limit flags、GitStore |
| 历史读写 | append_history（核心）、read_unprocessed_history、compact_history | strip_think 集成、oversize 日志、原子写、校验层 |
| 持久化文件 | — | read_memory/write_memory、read_soul/write_soul、read_user/write_user、get_memory_context |
| Dream | get/set_last_dream_cursor、build_dream_prompt（硬编码）、_render_current_memory_files | 模板系统、dream_content_diff、build_dream_tools、dream_run_completed、dream_session_key、build_dream_commit_message、prune_dream_sessions |
| 工具方法 | _read_entries、_read_cursor_counter、_read_last_entry、_format_messages（格式不同） | read_file、_valid_cursor、_iter_valid_entries、_valid_history_payload、_is_internal_history_session、read_recent_history_for_prompt |
| Legacy | — | 全部 7 个迁移方法 |

### Consolidator（~75%）

| 类别 | 已对齐 | 差异 |
|------|--------|------|
| 核心流程 | pick_boundary、replay_overflow、archive、maybe_consolidate_by_tokens、compact_idle_session | token 估算用 sum 而非 estimate_session_prompt_tokens |
| 初始化 | store/sessions/build_messages/get_tool_definitions | 缺 unified_session、_locks 用 dict 而非 WeakValueDictionary |
| 工具方法 | _persist_last_summary、estimate_session_prompt_tokens | _truncate_to_token_budget 用 chars*4 粗估、_input_token_budget 用 runtime.max_tokens |
| LLM 调用 | 基本 chat 调用 | 系统提示硬编码、缺 finish_reason 检查、参数不完整 |

### AutoCompact（~90%）
基本对齐，异常处理更健壮，仅缺部分日志。

---

## 依赖缺失清单

| 依赖 | 参考实现位置 | step91 状态 | 引入 step |
|------|------------|------------|----------|
| `truncate_text_to_tokens()` | utils/helpers.py | 缺失 | step98 |
| `workspace_prompts` 模块 | utils/workspace_prompts.py | 缺失 | step101 |
| `GitStore` 类 | utils/gitstore.py | 缺失 | step105 |
| `render_template()` | utils/prompt_templates.py | 缺失 | step99（内联模板） |

---

## Step 规划总览

### 阶段一：MemoryStore 持久化文件 API（P0）

#### step92：持久化文件读写基础方法
- **目标**：新增 read_file 静态方法 + MEMORY.md/SOUL.md/USER.md 的 6 个读写方法
- **修改文件**：`memory.py`
- **新增**：`read_file(path)`、`read_memory()`、`write_memory(content)`、`read_soul()`、`write_soul(content)`、`read_user()`、`write_user(content)`
- **验收**：文件不存在时 read 返回空串；write 覆盖写入；UTF-8 编码

#### step93：get_memory_context + context.py 集成
- **目标**：长期记忆注入 system prompt
- **修改文件**：`memory.py`、`context.py`
- **新增**：`get_memory_context()` → `## Long-term Memory\n{content}` 或空串
- **验收**：MEMORY.md 有内容时 system prompt 包含长期记忆段；空文件不注入；`include_memory_recent_history=False` 跳过

### 阶段二：数据健壮性（P0）

#### step94：_write_entries 原子写
- **目标**：history.jsonl 写入改为原子操作
- **修改文件**：`memory.py`
- **改动**：tmp → fsync → os.replace → 目录 fsync（Windows 跳过）；异常清理 tmp
- **验收**：写入后内容正确；模拟中断不损坏原文件

#### step95：append_history 安全增强
- **目标**：strip_think 防模板泄漏 + oversize 日志限流 + 空内容处理
- **修改文件**：`memory.py`
- **新增字段**：`_oversize_logged`
- **验收**：含 `<think` 泄漏内容被清理；超限只警告一次

#### step96：数据校验层 + cursor 对齐
- **目标**：entry 校验 + cursor 单调性
- **修改文件**：`memory.py`
- **新增**：`_valid_cursor()`、`_valid_history_payload()`、`_iter_valid_entries()`
- **新增字段**：`_corruption_logged`、`_malformed_entry_logged`
- **改动**：`read_unprocessed_history` 改用 `_iter_valid_entries`；`get_latest_cursor` 改为 `max(_next_cursor()-1, 0)`；`_next_cursor` 改用校验层
- **验收**：无效 cursor/畸形 payload 被跳过且只警告一次

### 阶段三：会话过滤与上下文注入（P1）

#### step97：内部会话过滤 + 近期历史注入
- **目标**：区分内部会话，按 session_key 过滤历史
- **修改文件**：`memory.py`、`context.py`
- **新增常量**：`_INTERNAL_HISTORY_SESSION_PREFIXES`、`_INTERNAL_HISTORY_SESSION_KEYS`
- **新增方法**：`_is_internal_history_session()`、`read_recent_history_for_prompt()`
- **验收**：指定 session_key 只返回该会话；unified_session 合并非内部会话；内部会话不泄漏

### 阶段四：Consolidator 精度对齐（P1）

#### step98：truncate_text_to_tokens + 截断预算对齐
- **目标**：从 chars*4 粗估改为精确 token 截断
- **修改文件**：`helpers.py`、`consolidation.py`
- **新增函数**：`truncate_text_to_tokens(text, max_tokens)`
- **改动**：`_truncate_to_token_budget` 改用新函数；`_input_token_budget` 对齐
- **验收**：截断后 token 不超预算；budget<=0 回退

#### step99：archive 系统提示模板化 + LLM 调用对齐
- **目标**：硬编码提示改为模板，LLM 调用参数对齐
- **修改文件**：`consolidation.py`（+ 模板）
- **改动**：系统提示模板化；增加 temperature/reasoning_effort/tools/tool_choice；检查 finish_reason
- **验收**：摘要生成正常；LLM error 时触发 raw_archive

#### step100：token 估算对齐 + unified_session + WeakValueDictionary
- **目标**：完整 prompt 估算 + 锁结构对齐
- **修改文件**：`consolidation.py`
- **改动**：`maybe_consolidate_by_tokens` 用 `estimate_session_prompt_tokens`；`__init__` 加 `unified_session`；`_locks` 改用 `WeakValueDictionary`
- **验收**：token 判断含 system+tools 开销；锁随对象释放

### 阶段五：Dream 模板系统（P2）

#### step101：workspace_prompts 模块 + MemoryStore 模板方法
- **目标**：workspace 级 dream prompt 覆盖
- **修改文件**：新增 `utils/workspace_prompts.py`、修改 `memory.py`
- **新增模块**：`workspace_prompt_file()`、`has_workspace_prompt_override()`、`load_workspace_prompt_override()`、`WORKSPACE_PROMPT_MAX_CHARS`
- **新增方法**：`dream_prompt_file` property、`has_dream_prompt_override()`、`default_dream_prompt()`、`_dream_template()`
- **新增字段**：`_dream_prompt_oversize_logged`
- **验收**：workspace dream.md 存在时用自定义；超限截断警告

#### step102：build_dream_prompt 模板化
- **目标**：硬编码 prompt 改为 `_dream_template()` 渲染
- **修改文件**：`memory.py`
- **验收**：dream prompt 结构与参考一致；模板覆盖生效

### 阶段六：Dream 会话与运行管理（P2）

#### step103：dream_session_key + prune_dream_sessions
- **目标**：会话键标准化 + 旧会话清理
- **修改文件**：`memory.py`、`main.py`
- **新增方法**：`dream_session_key()`、`prune_dream_sessions()`
- **改动**：`main.run_dream` 改用新方法；流程结束清理旧会话
- **验收**：键格式统一；旧 dream 会话超 keep 数被清理

#### step104：dream_run_completed + build_dream_commit_message
- **目标**：运行完成判断 + 基于 diff 的 commit message
- **修改文件**：`memory.py`、`main.py`
- **新增方法**：`dream_run_completed(resp)`、`build_dream_commit_message(prefix, diff_body)`
- **改动**：`main.run_dream` 用 `dream_run_completed` 判断是否推进 cursor
- **验收**：未完成不推进 cursor；commit message 格式正确

### 阶段七：Git 集成 + Dream 工具集（P3）

#### step105：gitstore 模块
- **目标**：GitStore 工具类
- **修改文件**：新增 `utils/gitstore.py`
- **核心**：`is_initialized()`、`summarize_working_tree(paths)`、跟踪文件管理
- **验收**：git 仓库返回差异摘要；非 git 环境返回空/False

#### step106：MemoryStore Git 集成 + dream_content_diff
- **目标**：GitStore 集成 + 真实 diff 摘要
- **修改文件**：`memory.py`
- **改动**：`__init__` 创建 `self._git`；新增 `git` property；新增 `dream_content_diff()`
- **验收**：返回 SOUL/USER/MEMORY 未提交变更摘要；git 不可用返回空串

#### step107：build_dream_tools
- **目标**：Dream 受限工具集
- **修改文件**：`memory.py`
- **新增方法**：`build_dream_tools()` → ToolRegistry（Read/Edit/ApplyPatch/Write）
- **验收**：工具集只允许 memory 文件 + skills 目录

### 阶段八：Legacy 迁移 + 格式统一（P3）

#### step108：Legacy HISTORY.md 迁移
- **目标**：旧版 HISTORY.md 自动升级
- **修改文件**：`memory.py`
- **新增方法**：`_maybe_migrate_legacy_history()` 等 7 个方法
- **新增常量**：`_LEGACY_ENTRY_START_RE`、`_LEGACY_TIMESTAMP_RE`、`_LEGACY_RAW_MESSAGE_RE`
- **新增字段**：`legacy_history_file`
- **验收**：HISTORY.md 存在且 jsonl 为空时自动迁移；迁移后重命名 .bak；cursor 设为最后一条

#### step109：_format_messages + raw_archive 格式对齐
- **目标**：消息格式化一致 + raw_archive 带计数和日志
- **修改文件**：`memory.py`、`consolidation.py`
- **改动**：`_format_messages` 改为 `[timestamp] ROLE [tools: ...]: content`；`raw_archive` 用 `public_history_messages` + 计数 + logger.warning
- **验收**：归档格式一致；raw_archive 输出降级警告

### 阶段九：收尾

#### step110：整体回归 + 文档
- **目标**：全量测试通过 + 文档更新
- **修改文件**：`memory.py`、`consolidation.py`、配套文档
- **验收**：所有 memory/consolidation/autocompact 测试通过；对齐度 ≥97%

---

## 依赖关系

```
step92 → step93
step94, step95, step96（可并行，但按顺序实施）
step96 → step97
step98 → step99 → step100
step101 → step102
step103 → step104
step105 → step106 → step107
step108（独立）
step96+step97 → step109
step110（依赖全部）
```

## 对齐度里程碑

| Step | MemoryStore | Consolidator |
|------|------------|-------------|
| 91（当前） | ~45% | ~75% |
| 93 | ~55% | ~75% |
| 96 | ~70% | ~75% |
| 97 | ~78% | ~75% |
| 100 | ~78% | ~92% |
| 102 | ~83% | ~92% |
| 104 | ~88% | ~92% |
| 107 | ~94% | ~92% |
| 109 | ~97% | ~95% |
| 110 | ~98%+ | ~97%+ |
