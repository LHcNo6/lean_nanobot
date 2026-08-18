# Step 56: media 处理

## 目标

处理 InboundMessage.media 附件列表：
1. 非图片附件作为 `[Attachment: path]` 引用追加到消息内容
2. 图片作为 `[image: path]` 占位符追加到消息内容
3. 新增 `_prepare_message_media` 方法

## 最小增量方案

### 新增 utils/document.py
- `is_image_file(path)` - mimetypes 扩展判断图片
- `reference_non_image_attachments(content, media)` - 分离图片和非图片附件

### 修改 helpers.py
- 添加 `image_placeholder_text(path, empty="[image]")`

### 修改 loop.py
- 新增 `_prepare_message_media(content, media)` 方法
- 在 `_state_init` 中调用，处理 msg.media
- 图片路径通过 image_placeholder_text 转为占位符

## 不做
- 不实现文档文本提取（PDF/docx/xlsx/pptx 需要额外依赖）
- 不实现 base64 图片编码（需要 vision provider 支持）
- 不实现 channels_config.extract_document_text 配置
