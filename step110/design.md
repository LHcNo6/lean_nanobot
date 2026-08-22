# Step 110 Design: Memory 模块整体回归与文档收尾

## 实现思路

step110 是 memory 对齐主题的收尾 step，不新增功能，专注于验证与文档。

### 1. 回归测试策略

按测试层级分组验证：

| 层级 | 测试文件 | 覆盖范围 |
|------|---------|---------|
| MemoryStore 基础 | test_memory_store.py | read_file / read_memory / write_memory / read_soul / write_soul / read_user / write_user |
| MemoryStore 上下文 | test_memory_context.py | get_memory_context / context.py 集成 |
| MemoryStore 原子写 | test_memory_atomic_write.py | _write_entries 原子写 / crash-safety |
| MemoryStore 安全 | test_memory_append_safety.py | strip_think 集成 / oversize 日志限流 / 空内容处理 |
| MemoryStore 校验 | test_memory_validation.py | _valid_cursor / _iter_valid_entries / _valid_history_payload / 损坏日志限流 |
| MemoryStore 会话过滤 | test_memory_session_filter.py | _is_internal_history_session / read_recent_history_for_prompt / context 集成 |
| MemoryStore Git | test_memory_git.py | GitStore 集成 / dream_content_diff |
| Dream 模板 | test_dream_template.py | workspace_prompts 模块 / dream_prompt_file / has_dream_prompt_override / default_dream_prompt / _dream_template |
| Dream prompt | test_build_dream_prompt.py | build_dream_prompt 模板化 / 文件嵌入 / 历史注入 |
| Dream 工具 | test_build_dream_tools.py | build_dream_tools 受限工具集 |
| Dream 运行 | test_dream_run_helpers.py | dream_run_completed / build_dream_commit_message |
| Dream 会话 | test_dream_session.py | dream_session_key / prune_dream_sessions |
| Consolidator 归档 | test_consolidator_archive.py | archive 模板化 / LLM 调用参数 / finish_reason 检查 |
| Consolidator token | test_consolidator_tokens.py | unified_session / WeakValueDictionary / estimate_session_prompt_tokens |
| Consolidator 回放 | test_consolidation_replay.py | _replay_overflow_boundary / _consolidate_replay_overflow / maybe_consolidate_by_tokens |
| 格式对齐 | test_format_raw_archive.py | _format_messages 格式 / raw_archive 消息计数与日志 |
| Legacy 迁移 | test_legacy_migration.py | HISTORY.md → history.jsonl 自动迁移 |
| GitStore | test_gitstore.py | GitStore 初始化 / auto_commit / summarize_working_tree |
| Token 截断 | test_truncate_tokens.py | truncate_text_to_tokens 函数 |

### 2. BOM 头修复

memory.py 文件开头存在 UTF-8 BOM（EF BB BF），Python 虽可容忍但不符合项目编码规范。修复方式：以无 BOM 的 UTF-8 重新写入文件。

### 3. 回归基线对比

将 step110 全量测试结果与 step91（memory 改动前基线）对比，确认 30 个失败测试均为预先存在的平台/环境问题（bwrap 沙箱、Unix 路径、openai 依赖等），无 memory 改动引入的新增回归。

## 文件修改清单

| 文件 | 操作 |
|------|------|
| `memory.py` | 修改：移除 UTF-8 BOM 头 |
| `proposal.md` | 重写：step110 正确内容 |
| `design.md` | 重写：step110 正确内容 |
| `api-spec.md` | 重写：step110 最终 API 状态 |
| `step110.md` | 新建：memory 对齐总结文档 |
