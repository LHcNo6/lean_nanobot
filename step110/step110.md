# step110：Memory 模块整体回归与文档收尾

## 一、这一阶段解决了什么问题

step92-step110 完成了 memory 模块从 ~45% 到 ~98% 的对齐。step110 作为收尾 step，解决以下问题：

1. **全量回归验证**：确认 step92-step109 逐步实现的所有 memory 功能在整合后正常工作，无相互冲突
2. **编码规范修复**：修复 memory.py 的 UTF-8 BOM 头问题
3. **文档补全**：修正 step110 的 proposal/design/api-spec 规范文档，编写完整的配套总结文档

## 二、原理思路与具体实现

### 2.1 回归测试策略

采用分层验证策略，按功能域分组运行 19 个测试文件、214 个测试用例：

- **MemoryStore 基础层**：文件读写、cursor 管理、历史追加/读取
- **健壮性层**：原子写、strip_think 防泄漏、数据校验、损坏日志限流
- **上下文注入层**：get_memory_context、read_recent_history_for_prompt 与 context.py 集成
- **Dream 系统层**：模板覆盖、prompt 构建、受限工具集、会话管理、commit message
- **Consolidator 层**：token 估算、归档、回放窗口压缩、空闲会话压缩
- **兼容层**：Legacy HISTORY.md 迁移、GitStore 集成、格式对齐

### 2.2 BOM 头修复

memory.py 文件开头存在 UTF-8 BOM（EF BB BF）。Python 3 虽可容忍 BOM，但不符合项目纯 UTF-8 编码规范，且可能导致某些工具（如 diff、git）显示异常。修复方式：以无 BOM 的 UTF-8 编码重新写入文件。

### 2.3 回归基线对比

将 step110 全量测试结果与 step91（memory 改动前基线）对比：
- step91 已存在的失败：bwrap 沙箱（Linux 专属）、Unix 路径格式、openai 模块依赖等，共 30 个
- step110 无新增失败，确认 memory 改动未引入回归

## 三、Memory 对齐路线总览（step92-step110）

| Step | 主题 | 核心功能 |
|------|------|---------|
| step92 | 持久化文件 API | read_file + read_memory/write_memory + read_soul/write_soul + read_user/write_user |
| step93 | 长期记忆注入 | get_memory_context + context.py 集成 |
| step94 | 原子写 | _write_entries 改为 tmp + fsync + replace 的原子操作 |
| step95 | append 安全增强 | strip_think 集成 + oversize 日志限流 + 空内容处理 |
| step96 | 数据校验层 | _valid_cursor + _iter_valid_entries + _valid_history_payload + get_latest_cursor 对齐 |
| step97 | 会话过滤 | _is_internal_history_session + read_recent_history_for_prompt + context 集成 |
| step98 | token 精确截断 | helpers 新增 truncate_text_to_tokens + Consolidator 截断对齐 |
| step99 | 归档模板化 | archive 系统提示模板化 + LLM 调用参数对齐 + finish_reason 检查 |
| step100 | token 估算对齐 | estimate_session_prompt_tokens + unified_session + WeakValueDictionary |
| step101 | workspace_prompts | 新增 utils/workspace_prompts.py + MemoryStore dream 模板方法 |
| step102 | dream prompt 模板化 | build_dream_prompt 改用 _dream_template 替代硬编码 |
| step103 | dream 会话管理 | dream_session_key + prune_dream_sessions + main.py 集成 |
| step104 | dream 运行判断 | dream_run_completed + build_dream_commit_message |
| step105 | GitStore 模块 | 新增 utils/gitstore.py（init/auto_commit/summarize_working_tree） |
| step106 | Git 集成 | MemoryStore.__init__ 集成 GitStore + git property + dream_content_diff |
| step107 | dream 工具集 | build_dream_tools 受限工具集（read_file/write_file/edit_file，OpenAI function 格式列表） |
| step108 | Legacy 迁移 | HISTORY.md → history.jsonl 迁移（migrate_legacy_history 显式调用，7 个方法 + 3 个正则） |
| step109 | 格式统一 | _format_messages 单行格式对齐 + raw_archive 消息计数与日志 |
| step110 | 回归与文档 | 全量测试 + BOM 修复 + 文档收尾 |

## 四、核心类/方法功能说明

### MemoryStore（memory.py，~34KB）

纯文件 I/O 层，负责 memory/MEMORY.md、history.jsonl、SOUL.md、USER.md 的读写和 Dream 系统支撑。

**核心能力：**
- **history.jsonl 追加式存储**：cursor 单调递增，原子写保证 crash-safety，strip_think 防模板泄漏
- **持久化记忆文件**：MEMORY.md（长期事实）、SOUL.md（人格）、USER.md（用户画像）的统一读写
- **数据校验**：_iter_valid_entries 遍历校验，无效 cursor/畸形 payload 自动跳过并限流警告
- **Dream 系统**：模板化 prompt 构建、workspace 覆盖、Git 差异摘要、受限工具集、会话管理
- **Legacy 兼容**：自动将旧版 HISTORY.md 迁移为 history.jsonl，迁移后备份原文件

### Consolidator（consolidation.py，~20KB）

轻量级 token 预算驱动的会话压缩器。

**核心能力：**
- **token 预算压缩**：maybe_consolidate_by_tokens 循环归档旧消息，直到 prompt 低于安全预算
- **回放窗口压缩**：_consolidate_replay_overflow 归档超出 replay_max_messages 的消息
- **空闲会话压缩**：compact_idle_session 保留最近 N 条合法后缀，归档其余
- **LLM 摘要归档**：archive 调用 LLM 生成摘要，失败时 raw_archive 回退
- **精确 token 控制**：truncate_text_to_tokens 按 token 截断，estimate_session_prompt_tokens 完整估算

### AutoCompact（autocompact.py，~6KB）

空闲会话主动压缩器，与参考实现基本完全对齐。

### 新增依赖模块

| 模块 | 说明 |
|------|------|
| utils/gitstore.py | Git 操作封装（init/auto_commit/summarize_working_tree） |
| utils/workspace_prompts.py | workspace 级 prompt 覆盖机制 |
| helpers.truncate_text_to_tokens | 按 token 数截断文本 |

## 五、文件修改清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `memory.py` | 修改：移除 UTF-8 BOM 头 | 编码规范修复 |
| `proposal.md` | 重写 | step110 正确内容 |
| `design.md` | 重写 | step110 正确内容 |
| `api-spec.md` | 重写 | memory 模块最终 API 状态 |
| `step110.md` | 新建 | 本文档 |

## 六、测试结果

### Memory 相关测试（全部通过）

```
214 passed in 6.56s
```

覆盖 19 个测试文件：
- test_memory_store.py（17）
- test_memory_context.py
- test_memory_atomic_write.py
- test_memory_append_safety.py
- test_memory_validation.py
- test_memory_session_filter.py
- test_memory_git.py
- test_build_dream_prompt.py
- test_build_dream_tools.py
- test_dream_run_helpers.py
- test_dream_session.py
- test_dream_template.py
- test_consolidator_archive.py
- test_consolidator_tokens.py
- test_consolidation_replay.py
- test_format_raw_archive.py
- test_legacy_migration.py
- test_gitstore.py
- test_truncate_tokens.py

### 全量测试

```
1082 passed, 25 failed
```

25 个失败均为非 memory 模块的预先存在平台/环境问题（方向与 step91 基线一致）：

| 模块 | 数量 | 说明 |
|------|------|------|
| runner_robustness（重试分类/模式） | 8 | 预先存在 |
| workspace_tool（read_file context/boundary） | 5 | 预先存在 |
| runner_finalization | 3 | 预先存在 |
| events（心跳/事件路由） | 3 | 预先存在 |
| exec_session / exec_enhanced（Unix 路径等） | 3 | 预先存在 |
| runtime_context | 2 | 预先存在 |
| sandbox（bwrap，Linux 专属） | 1 | Windows 必然失败 |

**确认：memory 域 19 个测试文件全部通过，未引入任何新增回归。**

> 注：默认配置下全量运行在格式化失败详情时会触发 pytest INTERNALERROR
> （AST recursion depth mismatch，pytest 9.1.1 + Python 3.11 环境问题），
> 需加 `--tb=no` 方可完整跑完。本数字为本机实测；文档初稿记录的
> `1037 passed, 30 failed` 与当前环境存在约 40 用例的环境差异。

## 七、对齐度总结

| 模块 | step91 对齐度 | step110 对齐度 |
|------|-------------|---------------|
| MemoryStore | ~45% | ~98% |
| Consolidator | ~75% | ~97% |
| AutoCompact | ~90% | ~95% |
| 整体 | ~60% | ~97% |

### 剩余微小差异（非阻塞）

1. Consolidator.archive 的 LLM 调用使用 `runtime.provider.chat` 而非 `chat_with_retry`（重试逻辑在 provider 层已处理）
2. 部分日志使用 `logging` 模块而非 `loguru`（项目整体未引入 loguru）
3. Dream 流程的自动 commit 未完全集成（需要 command/builtin 层配合，属于 harness 范围）

## 八、暴露的问题

1. **全量测试环境依赖**：文档初稿环境中 test_config.py 因缺 openai 模块在收集阶段中断；本次验证环境已安装 openai，test_config 正常通过。建议后续 step 为该依赖添加 mock 或条件跳过以保证可复现性。
2. **平台相关测试**：bwrap、Unix 路径等测试在 Windows 上必然失败，建议添加平台 skip 标记。
3. **规范文档复制错误**：step109-step110 的 proposal/design/api-spec 初始内容均为 step100 的复制，说明批量创建 step 时需要更严格的文档校验。

## 九、下一步

memory 核心主题对齐已完成。后续可考虑：
1. **harness 层 Dream 集成**：将 dream_content_diff、build_dream_commit_message、auto_commit 集成到 command/builtin 层
2. **SDK 层 memory API**：对齐 nanobot sdk/clients.py 中的 read_memory/write_memory 接口
3. **测试环境优化**：修复平台相关测试的 skip 标记，添加 openai mock
4. **继续 agent 其他模块对齐**：如 context_governance、automation_turns 等
