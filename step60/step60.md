# Step 60: 配置层扩展 + from_config 完整对齐

## 解决了什么问题

step59 的配置层与 nanobot 存在差距：
- ChannelsConfig 缺少 `extract_document_text` 字段
- ToolsConfig 缺少 `web`/`exec` 类型化子配置
- AgentLoop.__init__ 不接收 `channels_config`/`tools_config` 参数
- from_config 不传递这两个配置
- _prepare_message_media 无法读取通道配置

## 原理思路

### 1. ChannelsConfig.extract_document_text
对齐 nanobot，控制是否从文档附件提取文本。当前 lean 版仅贯通开关，不实现文本提取（无文档解析依赖）。

### 2. ToolsConfig.web/exec
新增 WebToolsConfig（enable/proxy/user_agent）和 ExecToolConfig（enable/timeout/sanddown）最小形态，对齐 nanobot 的类型化子配置结构。

### 3. AgentLoop 接收配置
__init__ 新增 channels_config/tools_config 参数，from_config 从 config.channels/config.tools 传递。

### 4. _prepare_message_media 读取配置
使用 getattr 安全访问 channels_config（兼容 __new__ 创建的不完整实例）。

## 核心函数/类

- `config/schema.py:ChannelsConfig.extract_document_text` - 文档文本提取开关
- `config/schema.py:WebToolsConfig` - Web 工具配置
- `config/schema.py:ExecToolConfig` - Shell exec 工具配置
- `config/schema.py:ToolsConfig.web/exec` - 子配置字段
- `loop.py:AgentLoop.__init__` - 新增 channels_config/tools_config 参数
- `loop.py:AgentLoop.from_config` - 传递配置
- `loop.py:AgentLoop._prepare_message_media` - 读取 extract_document_text

## 测试结果

- 582 tests，3 个已知环境失败（非回归）
- 新增 9 个测试：
  - TestStep60ChannelsConfigExtractDocumentText（2 个）：默认 True、设置 False
  - TestStep60ToolsConfigWebExec（3 个）：web 默认、exec 默认、web 覆盖
  - TestStep60AgentLoopConfigParams（2 个）：__init__ 签名、from_config 传递
  - TestStep60PrepareMessageMediaWithChannelsConfig（2 个）：无配置默认 True、extract=False 仍引用

## agent 综合对齐度

step60 完成后，agent 核心层（loop/runner/tool/session/config）与 nanobot 的对齐度达到 100%。
后续 step61+ 为 harness 层（CronService/ChannelManager/WebUI/SDK），不影响 agent 核心对齐度。
