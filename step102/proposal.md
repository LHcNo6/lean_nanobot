# Step 102 Proposal: build_dream_prompt 模板化

## 1. 问题背景

step101 已新增 `_dream_template()` 方法，但 `build_dream_prompt` 仍使用硬编码的 "You are a memory curator..." 文本作为 prompt 前缀，未使用模板机制。导致 workspace 覆盖不生效。

## 2. 目标

将 `build_dream_prompt` 中的硬编码 prompt 前缀替换为 `self._dream_template()`，使 Dream prompt 支持 workspace 级自定义覆盖。

## 3. 非目标

- 不修改 `_dream_template` 实现（step101 已完成）
- 不修改 files_section 和 history_text 的构建逻辑
- 不修改 Dream 运行流程

## 4. 验收标准

1. `build_dream_prompt` 使用 `_dream_template()` 作为 prompt 前缀
2. prompt 结构为 `{template}\n\n{files_section}\n\n## Conversation History\n{history_text}`
3. workspace 存在 dream.md 覆盖时，生成的 prompt 包含自定义内容
4. 无覆盖时使用默认模板
5. 无历史时返回 None
6. 单元测试通过
