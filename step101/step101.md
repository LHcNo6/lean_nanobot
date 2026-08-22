# step101：workspace_prompts 模块 + MemoryStore dream 模板方法

## 解决的问题

缺少 workspace 本地 prompt 覆盖机制，MemoryStore 没有 dream prompt 模板方法。

## 实现

1. 新增 `utils/workspace_prompts.py`：`workspace_prompt_file`、`load_workspace_prompt_override`、`has_workspace_prompt_override`、`initialize_workspace_prompt`
2. MemoryStore 新增 `dream_prompt_file`（property）、`has_dream_prompt_override`、`default_dream_prompt`（静态方法）、`_dream_template`
3. 新增 `_dream_prompt_oversize_logged` flag，超限首次 warning 后限流

## 文件修改清单

| 文件 | 操作 |
|------|------|
| `utils/workspace_prompts.py` | 新建 |
| `memory.py` | 修改：+导入 +flag +4 个 dream 模板方法 |
| `tests/test_dream_template.py` | 新建（16 测试） |
| 规范文档 + step101.md | 新建 |

## 测试结果

16 passed in 0.28s

## 下一步

**step102**：build_dream_prompt 改用 `_dream_template()` 替代硬编码 prompt。
