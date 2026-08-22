# Step 101 Proposal: workspace_prompts 模块 + MemoryStore dream 模板方法

## 1. 问题背景

Dream 系统的 prompt 当前硬编码在 `build_dream_prompt` 中，无法被 workspace 级配置覆盖。nanobot 支持通过 workspace 下的 `.prompts/dream.md` 文件自定义 Dream prompt，且有最大字符数限制和超限截断机制。当前缺少：
1. `utils/workspace_prompts.py` 模块（workspace prompt 文件路径解析、覆盖检测、加载截断）
2. MemoryStore 的 dream 模板方法（`dream_prompt_file`、`has_dream_prompt_override`、`default_dream_prompt`、`_dream_template`）

## 2. 目标

1. 新增 `utils/workspace_prompts.py` 模块，提供 workspace prompt 覆盖机制
2. MemoryStore 新增 4 个 dream 模板相关方法/属性
3. 超限日志限流（`_dream_prompt_oversize_logged` flag）

## 3. 非目标

- 不修改 `build_dream_prompt`（step102 完成）
- 不实现 Dream 运行逻辑（已有 main.run_dream）

## 4. 验收标准

1. `workspace_prompt_file(workspace, name)` 返回 `.prompts/{name}.md` 路径
2. `has_workspace_prompt_override(path)` 文件存在且非空时返回 True
3. `load_workspace_prompt_override(path)` 返回 `(text, original_chars)`，超限截断
4. `MemoryStore.dream_prompt_file` 返回正确路径
5. `MemoryStore.has_dream_prompt_override()` 正确检测
6. `MemoryStore.default_dream_prompt()` 返回非空默认模板
7. `MemoryStore._dream_template()` 优先使用覆盖，否则用默认
8. 超限首次 warning，后续限流
9. 单元测试通过
