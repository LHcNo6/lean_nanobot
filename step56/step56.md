# Step 56: media 处理

## 解决了什么问题

step55 中 `InboundMessage.media` 字段存在但完全未被使用。用户发送的附件（图片、文档等）在构建 LLM 消息时被忽略。nanobot 通过 `_prepare_message_media` 将非图片附件作为引用追加到消息内容，图片路径保留供下游 vision block 处理。

## 原理思路

### 附件分类处理

1. **非图片附件**（PDF/txt/docx 等）：不读取文件内容，以 `[Attachment: path]` 文本引用追加到 content
2. **图片附件**（png/jpg/gif/webp）：路径保留在 media 列表中，同时以 `[image: path]` 占位符追加到 content（step56 不实现 base64 vision 编码）

### 新增模块

- `utils/document.py`：
  - `is_image_file(path)` - mimetypes 扩展判断图片
  - `reference_non_image_attachments(content, media)` - 分离图片和非图片附件
- `helpers.py:image_placeholder_text(path, empty="[image]")` - 图片占位符文本

### loop.py 改动

- 新增 `_prepare_message_media(content, media)` 方法（委托 reference_non_image_attachments）
- `_build_initial_messages` 中，当 `current_role == "user"` 且 `msg.media` 非空时：
  1. 调用 `_prepare_message_media` 分离图片和附件
  2. 非图片附件引用追加到 content
  3. 图片占位符追加到 content

### 最小增量边界

- 不实现文档文本提取（PDF/docx/xlsx/pptx 需要 pypdf、python-docx 等依赖）
- 不实现 base64 图片编码（需要 vision provider 支持）
- 不实现 channels_config.extract_document_text 配置
- 不实现 magic-byte 图片检测（仅 mimetypes 扩展判断）

## 核心函数/类

- `utils/document.py:is_image_file(path)` - 图片文件判断
- `utils/document.py:reference_non_image_attachments(content, media)` - 附件分离
- `helpers.py:image_placeholder_text(path, empty)` - 图片占位符
- `loop.py:AgentLoop._prepare_message_media(content, media)` - 媒体处理入口

## 测试结果

- 544 tests，3 个已知环境失败（非回归）
- 新增 12 个测试：
  - TestStep56DocumentUtils（6 个）：is_image_file、reference_non_image_attachments 各种场景
  - TestStep56ImagePlaceholder（4 个）：占位符文本生成
  - TestStep56PrepareMessageMedia（2 个）：方法存在性和附件分离

## 暴露的问题

- 图片仅以文本占位符表示，完整 vision 支持需要 base64 编码和多模态消息格式，留待后续。
- 文档文本提取未实现，用户发送 PDF/docx 等文件时 LLM 只能看到文件路径引用。

## 下一 step

step57：TurnContext 字段重构 + 技术债清理（移除 result/error/summary、_state_run 不再重建 ctx.result、_process_message 异常直接 raise）。
