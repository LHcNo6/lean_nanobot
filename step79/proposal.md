# Step 79 Proposal: ImageGenerationTool

## 1. 问题背景

agent 无法生成图片。nanobot 的 ImageGenerationTool 通过配置的 provider 生成图片并保存为 artifact。

## 2. 目标

新建 `tools/image_generation.py`，实现简化版 ImageGenerationTool：
1. 支持 prompt/reference_images/aspect_ratio/image_size/count 参数
2. 可插拔 provider 抽象（ImageGenerationProvider ABC）
3. 默认 SimpleProvider 生成占位 SVG 图片（无外部依赖）
4. 保存到 workspace/generated 目录
5. 返回图片路径和元数据

## 3. 非目标

- 不实现真实的 AI 图片生成（需要外部 API）
- 不实现图片编辑（reference_images 只存储不处理）
- 不实现 artifact 系统

## 4. 验收标准

1. ImageGenerationTool 可被 ToolLoader 发现
2. prompt 生成图片成功
3. 图片保存到 generated 目录
4. count 生成多张图片
5. aspect_ratio 影响输出尺寸
6. 单元测试通过
