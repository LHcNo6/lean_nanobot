# Step 60: 配置层扩展 + from_config 完整对齐

## 目标

1. ChannelsConfig 添加 extract_document_text 字段
2. ToolsConfig 添加 web/exec 子配置
3. AgentLoop.__init__ 添加 channels_config/tools_config 参数
4. from_config 传递 channels_config/tools_config
5. _prepare_message_media 使用 channels_config.extract_document_text

## 最小增量方案

### config/schema.py
- ChannelsConfig: + extract_document_text: bool = True
- ToolsConfig: + web: dict (extra allow 已支持), + exec: dict
- 新增 WebToolsConfig/ExecToolConfig 简单 pydantic model

### loop.py
- __init__: + channels_config, + tools_config 参数
- from_config: 传递 channels_config=config.channels, tools_config=config.tools
- _prepare_message_media: 使用 self.channels_config.extract_document_text 判断
