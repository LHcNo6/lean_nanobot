# Step 110 Proposal: Memory 模块整体回归与文档收尾

## 1. 问题背景

step92-step109 已逐步完成 MemoryStore、Consolidator、Dream 系统、Git 集成、Legacy 迁移等全部 memory 核心功能的对齐。step110 作为 memory 主题的收尾 step，需要：
1. 全量回归测试，确认所有 memory 相关功能正常且无回归
2. 修复遗留的小问题（如 BOM 头）
3. 更新配套文档，总结整个 memory 对齐路线的成果

## 2. 目标

1. 运行全量测试，确认 memory 相关 214 个测试全部通过
2. 确认非 memory 测试无新增回归（与 step91 基线对比）
3. 修复 memory.py 的 UTF-8 BOM 头问题
4. 编写完整的 step110.md 配套文档，总结 memory 模块对齐成果
5. 更新 proposal.md / design.md / api-spec.md 为 step110 正确内容

## 3. 非目标

- 不新增任何 memory 功能（功能对齐已在 step92-step109 完成）
- 不修改 memory 核心逻辑
- 不处理与 memory 无关的预先存在测试失败（如 bwrap/Linux 路径/openai 依赖等）

## 4. 验收标准

1. memory 相关 19 个测试文件、214 个测试用例全部通过
2. 全量测试中无 memory 改动引入的新增失败
3. memory.py 无 BOM 头，编码为纯 UTF-8
4. step110.md 文档完整，包含对齐度总结、文件修改清单、测试结果
5. 所有规范文档（proposal/design/api-spec）内容与 step110 一致
