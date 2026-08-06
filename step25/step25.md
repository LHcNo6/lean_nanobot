# Step 25 — Pydantic 配置系统（H1）

在 Step 24 (Session 持久化净化 + Checkpoint 恢复) 基础上，对齐 nanobot 的
`config/schema.py` + `config/loader.py` 最小集，消除 main.py 硬编码，
工厂改接 Config，落地 `Tool.config_cls()`，搭起 `AgentLoop.from_config` 装配雏形。

---

## 一、这一阶段解决了什么问题、为什么要这样做

Step 22–24 的装配全部散落在 `main.py`：`_DEMO_CONTEXT_WINDOW` / `_DEMO_MAX_TOKENS` /
`_PRESETS` / `OPENAI_*` env 直读 / `workspace="."` / `ChannelManager(config={...})` /
dream 间隔 300s 等都是硬编码常量；provider 工厂被 `ProviderSettings` dataclass 占住，
无法从配置文件驱动；`loop.py` 给工具传的 `ToolContext(config=None, workspace="")`
（todolist A10/A11 的缺口根源）导致工具拿不到真实配置。

nanobot 的做法（`nanobot/config/`）：
- `schema.py`：pydantic model 体系，`Base` 双写兼容 camel/snake，根 `Config` 聚合
  `agents.defaults` / `providers` / `channels` / `model_presets`，并提供
  `resolve_preset` / `get_provider*` 等查询方法；
- `loader.py`：`load_config`（无文件→默认；有文件→`_migrate_config`→validate）、
  `save_config`（`by_alias` 落盘、保留 `${VAR}` 模板）、`${VAR}` env 引用递归替换、
  `NANOBOT_` 前缀 env（`__` 嵌套分隔符）；
- `providers/factory.py` 以 `make_provider(config, preset_name=...)` 装配，`AgentLoop.from_config`
  在 `agent/loop.py` 统一装配。

本 step 对齐这条链路的最小集。

## 二、目标与实现

| 目标 | 实现 |
|------|------|
| schema | `config/schema.py`：`Base`（camel/snake 双写）；`ProviderConfig`（api_key/api_base/api_type）；`ModelPresetConfig`（命名预设 + `to_generation_settings`）；`AgentDefaults`（workspace / model / provider / 生成参数 / fallback_models / max_tool_result_chars / session_ttl_minutes / consolidation_ratio / disabled_skills / bot_name / dream）；`BaseSettings` 之外无 env 依赖；`Config` 聚合 + `resolve_preset` / `get_provider` / `get_provider_name` / `get_api_key` / `get_api_base` / `workspace_path` |
| loader | `config/loader.py`：`load_config` / `save_config` / `get/set_config_path` / `merge_missing_defaults`；`_env_to_config_dict` 手写 `NANOBOT_` 前缀 + `__` 嵌套解析，"文件优先、env 只补缺省"；`resolve_config_env_vars` 原地递归 `${VAR}` 替换（缺失抛 ValueError）；`_migrate_config` 丢弃 legacy `maxMessages`/`max_messages` |
| 工厂接 Config | `providers/factory.py` 双路分发（isinstance）：`Config` → `_make_provider_from_config`（resolve_preset → registry 匹配 → 凭据/端点校验 → 构造，`fallback_models` 逐级 FallbackProvider）；`ProviderSettings` → 遗留路径（388 回归零改动）。`provider_signature` / `build_provider_snapshot` 同样双路 |
| from_config | `loop.py:AgentLoop.from_config(config, bus, **extra)` 雏形：`make_provider(config)` → `LLMRuntime.capture`（resolve_preset 参数）→ workspace/session_ttl_minutes/max_tool_result_chars 注入（extra 可覆盖）；`__init__` 只加了可选 `config` 参数，向后兼容 |
| ToolContext | `_build_agent_spec` 把真实 `config` 与 `workspace_path` 注入 `ToolContext`（终结 `config=None, workspace=""`） |
| Tool.config_cls 落地 | `tool.py:Tool.resolve_tool_config(ctx)`：按 `config_cls()` 从 `ctx.config.tools.<config_key>` 解析类型化配置；`tools/echo.py` 演示：`EchoToolConfig` + `config_key="echo"` + `create/enabled` 读取真实配置 |
| main.py | 删全部硬编码：`load_config → resolve_config_env_vars → build_provider_snapshot → AgentLoop.from_config`；ChannelManager 接 `config.channels.channel_sections()`（cli 默认段兜底） |
| 测试 | `tests/test_config.py`（pytest，40 个，全构造数据）：schema 默认值/双写别名/预设校验/provider 匹配/自定义 provider；loader 文件加载/迁移/env 补缺与优先级/值强转/`${VAR}` 替换与缺失抛错/save 往返与模板保留；factory Config 路径（装配/回退/签名/快照/双路分发）；from_config；tool config 注入 |

## 三、核心函数 / 类说明

### `config/schema.py`
- `Base`：`alias_generator=to_camel, populate_by_name=True`（对齐 nanobot `config_base.Base`）。
- `Config.resolve_preset(name=None)`：None/""/"default` 走 `agents.defaults` 隐式预设，
  否则查 `model_presets`；`model_preset` 引用不存在的预设在校验期报错。
- `Config.get_provider/get_provider_name/get_api_key/get_api_base`：走 step22 registry
  （关键词匹配 > 强制 provider 名 > 自定义 extra），api_base 缺省回退 spec 默认。

### `config/loader.py`
- `load_config`：文件 JSON（优先）+ `NANOBOT_` env（补缺）合并后整体 `model_validate`
  （值与类型由 pydantic 强转）；坏 JSON / 校验错误统一抛含路径的 ValueError。
- `_env_to_config_dict`：把 `NANOBOT_X__Y__Z` 转嵌套 dict（段名小写，值保留字符串待强转）。
- `save_config`：`model_dump(mode="json", by_alias=True)` 落盘，`${VAR}` 模板原样保留。

### `providers/factory.py`
- `make_provider / provider_signature / build_provider_snapshot` 三入口都做
  `is_config_input` 分发；Config 路径构造 `ProviderSnapshot` 时 context window 取主/回退最小。
- 遗留 `ProviderSettings` 路径整体保留（`_make_provider_from_settings` 等），388 回归不坏。

### `loop.py`
- `AgentLoop.from_config`：装配雏形；provider / registry / sessions / memory / identity /
  session_ttl_minutes / max_tool_result_chars 均可由 extra 覆盖。
- `_build_agent_spec`：`ToolContext(config=self.config, workspace=...)`。

### `tool.py` + `tools/echo.py`
- `Tool.resolve_tool_config(ctx)`：`config_cls()` → `ctx.config.tools.<config_key>` 实例化类型化配置。
- `EchoTool`：`config_key="echo"`，`enabled` 看配置开关，`execute` 应用 prefix/max_length。

## 四、暴露的问题 / 偏离与取舍

| 取舍 | 说明 | 后续计划 |
|------|------|----------|
| env 方案 | 不用 `pydantic_settings.BaseSettings`，手写 `_env_to_config_dict`（loader 内 ~25 行），语义 "**文件优先、env 只补缺省**"（nanobot 文件存在时其实是 env 全部忽略，语义更明确也更宽松） | — |
| fallback_models 形态 | nanobot 允许 "预设名 or 内联配置"，lean 只接受**模型名字符串**（直接按模型匹配 provider 并继承主预设生成参数） | 需要时可扩展 |
| provider 字段集 | 内置 provider 只做 step22 registry 六个条目（custom/openai/deepseek/dashscope/openrouter/ollama），无 extra_headers/extra_body 等；`api_type` 仅留字段 | step30 provider 收敛 |
| 工具配置形态 | nanobot `ToolsConfig` 是每工具命名类型字段（web/exec/file…）配 `model_rebuild` 解环；lean 用 `extra="allow"` 通用映射 + 工具自身 `config_cls()` 解析 | step30 工具体系再做类型化 |
| max_iterations 等 | `agents.defaults.max_tool_iterations / max_concurrent_subagents` 已入 schema，但 AgentLoop 构造器尚未消费（step25 只消 workspace/ttl/max_tool_result_chars） | runner 收敛时接上 |
| 全局 `_current_config_path` | 与 nanobot 相同（多实例/测试注入），多进程读同一路径时存在竞态 | — |
| 迁移范围 | 只做 nanobot 里与 lean 相关的 `maxMessages/max_messages` 丢弃；restrictToWorkspace 等迁移无对应字段不搬 | — |

## 五、下一步要解决什么

Step 26 — 事件层：typed outbound events（Progress / RetryWait / StreamEnd / …）+
进程内 RuntimeEventBus（H4 + H3），为真实通道/状态机观测铺路；`AgentLoop.from_config`
继续向 `channels.__init__` 的事件回调桥接演进。